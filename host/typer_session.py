#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/typer_session.py

Host side of the JSON-lines session with android/hid_typer.py.  The daemon runs
under ``adb shell su -c`` and is driven over stdin/stdout.  Liveness is
enforced in both directions: the host pings every 2 s (daemon watchdog), the
daemon heartbeats every 3 s (host stale-detector).
"""
import json
import queue
import threading
import time


class SessionDead(RuntimeError):
    """The daemon / adb transport is gone (recoverable)."""


class DaemonError(RuntimeError):
    def __init__(self, code, detail, event=None):
        RuntimeError.__init__(self, "%s: %s" % (code, detail))
        self.code = code
        self.detail = detail
        self.event = event or {}


class TyperSession(object):
    STALE_AFTER = 25.0

    def __init__(self, adb, python, env_prefix, remote_dir, device, pacing, extra_args="",
                 logger=None, watchdog=15.0):
        self.adb = adb
        self.python = python
        self.env = env_prefix
        self.remote_dir = remote_dir
        self.device = device
        self.pacing = pacing
        self.extra = extra_args
        self.log = logger
        self.watchdog = watchdog
        self.proc = None
        self.events = queue.Queue()
        self.alive = threading.Event()
        self._wlock = threading.Lock()
        self._next_id = 1
        self.last_rx = time.monotonic()
        self.stderr_tail = []

    # -------------------------------------------------------------- life --
    def start(self, ready_timeout=25.0):
        cmd = ("%scd %s && exec %s %s/hid_typer.py serve --device %s --watchdog %s "
               "--log %s/hid_typer.log --lock %s/hid_typer.lock %s"
               % (self.env, self.remote_dir, self.python, self.remote_dir, self.device,
                  self.watchdog, self.remote_dir, self.remote_dir, self.extra))
        if self.log:
            self.log.debug("starting daemon: %s", cmd)
        self.proc = self.adb.popen_su(cmd)
        self.alive.set()
        self.last_rx = time.monotonic()
        for target in (self._read_stdout, self._read_stderr, self._ping_loop):
            threading.Thread(target=target, daemon=True).start()
        end = time.monotonic() + ready_timeout
        while time.monotonic() < end:
            try:
                ev = self.events.get(timeout=0.5)
            except queue.Empty:
                if not self.alive.is_set():
                    break
                continue
            if ev.get("event") == "ready":
                if ev.get("uid") not in (0, -1, None):
                    self.close()
                    raise DaemonError("not_root", "daemon runs as uid %s" % ev.get("uid"))
                self.request({"cmd": "configure", "wpm": self.pacing["wpm"],
                              "pause_min": self.pacing["pause_min"],
                              "pause_max": self.pacing["pause_max"],
                              "pause_mean_interval": self.pacing["pause_mean_interval"],
                              "hold_ms": self.pacing["hold_ms"]}, timeout=10)
                return ev
            if ev.get("event") == "error":
                self.close()
                raise DaemonError(ev.get("code", "start_failed"), ev.get("detail", ""), ev)
        tail = " | ".join(self.stderr_tail[-5:])
        self.close()
        raise SessionDead("daemon did not become ready (%s)" % (tail or "no output"))

    def close(self):
        was_alive = self.alive.is_set()
        self.alive.clear()
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if was_alive and proc.poll() is None:
                try:
                    proc.stdin.write(b'{"cmd":"shutdown"}\n')
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        finally:
            for s in (proc.stdout, proc.stderr):
                try:
                    s.close()
                except Exception:
                    pass

    # --------------------------------------------------------- io threads --
    def _read_stdout(self):
        proc = self.proc
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self.last_rx = time.monotonic()
                try:
                    ev = json.loads(line)
                except ValueError:
                    if self.log:
                        self.log.debug("daemon stdout: %s", line)
                    continue
                self.events.put(ev)
        except (OSError, ValueError):
            pass
        finally:
            self.alive.clear()

    def _read_stderr(self):
        proc = self.proc
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self.stderr_tail.append(line)
                    del self.stderr_tail[:-30]
                    if self.log:
                        self.log.debug("daemon stderr: %s", line)
        except (OSError, ValueError):
            pass

    def _ping_loop(self):
        while self.alive.is_set():
            try:
                self._send({"cmd": "ping"})
            except SessionDead:
                return
            time.sleep(2.0)

    def _send(self, obj):
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("ascii")
        with self._wlock:
            proc = self.proc
            if proc is None or not self.alive.is_set():
                raise SessionDead("session closed")
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self.alive.clear()
                raise SessionDead("write to daemon failed: %s" % exc)

    # ------------------------------------------------------------ requests --
    def request(self, msg, timeout=None, abort_check=None, on_event=None):
        """Send a command and wait for its terminal event.

        Terminal events: ack, typed, aborted, error (matched by id).
        ``abort_check`` is polled ~2x per second; when it returns True an
        abort is sent once and the call returns the daemon's "aborted" event.
        ``timeout`` bounds the total wait (None = unbounded while the link is live).
        """
        cid = self._next_id
        self._next_id += 1
        msg = dict(msg, id=cid)
        while True:                                   # drop stale events
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self._send(msg)
        start = time.monotonic()
        aborted_sent = False
        while True:
            if timeout is not None and time.monotonic() - start > timeout:
                raise SessionDead("request %s timed out after %ss" % (msg.get("cmd"), timeout))
            if time.monotonic() - self.last_rx > self.STALE_AFTER:
                self.close()
                raise SessionDead("no data from daemon for %.0fs" % self.STALE_AFTER)
            try:
                ev = self.events.get(timeout=0.5)
            except queue.Empty:
                if not self.alive.is_set() and self.events.empty():
                    raise SessionDead("daemon connection closed (%s)"
                                      % (" | ".join(self.stderr_tail[-3:]) or "adb link lost"))
                if abort_check and not aborted_sent and abort_check():
                    aborted_sent = True
                    try:
                        self._send({"cmd": "abort", "id": cid})
                    except SessionDead:
                        raise
                continue
            kind = ev.get("event")
            if kind in ("hb", "log"):
                if kind == "log" and self.log:
                    self.log.info("daemon: %s", ev.get("msg"))
                continue
            if ev.get("id") not in (None, cid) and kind in ("ack", "typed", "aborted", "error", "progress"):
                continue
            if kind == "error" and ev.get("code") == "another_instance":
                raise DaemonError("another_instance", ev.get("detail", ""), ev)
            if kind in ("ack", "typed", "aborted"):
                return ev
            if kind == "error":
                return ev
            if on_event:
                on_event(ev)
            if abort_check and not aborted_sent and abort_check():
                aborted_sent = True
                self._send({"cmd": "abort", "id": cid})
