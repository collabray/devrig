#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/orchestrator.py

Automated hardware input-injection pipeline (Windows host).

  GitHub URL -> clone -> typing plan -> (for every file) create directories and
  file stub, open it in an isolated VS Code, type the content through the
  Android USB-HID keyboard, save, verify against the plan -> audit -> ZIP.

Everything is resumable: progress lives in ``state_manager.json``; re-running
the same command after a crash, power loss or ADB disconnect verifies the
workspace on disk and continues exactly where it stopped.

  python orchestrator.py https://github.com/owner/repo C:\\work\\repo-out
"""
import argparse
import logging
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "android"))

import adb_bridge  # noqa: E402
import packager  # noqa: E402
import repo_planner  # noqa: E402
import state_manager  # noqa: E402
import typer_session  # noqa: E402
import vscode_env  # noqa: E402
import win_focus  # noqa: E402

REMOTE_DIR = "/data/local/tmp/devrig"
VERSION = "1.0.0"


class FatalError(RuntimeError):
    """Unrecoverable: needs operator action."""


class Recoverable(RuntimeError):
    """Transient rig failure: reconnect, verify, resume."""


def common_prefix_len(a, b):
    n = min(len(a), len(b))
    if a[:n] == b[:n]:
        return n
    lo, hi = 0, n
    while lo < hi:                       # binary search on the first differing byte
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def fmt_dur(sec):
    sec = int(max(0, sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return ("%dd " % d if d else "") + "%02d:%02d:%02d" % (h, m, s)


class Orchestrator(object):
    def __init__(self, args):
        self.args = args
        self.scale = args.timing_scale
        self.ws = Path(args.workspace).resolve() if args.workspace else None
        if args.state_dir:
            self.ctrl = Path(args.state_dir).resolve()
        elif self.ws:
            self.ctrl = self.ws.parent / (".devrig_" + self.ws.name)
        else:
            self.ctrl = Path.cwd() / ".devrig"
        self.repo_dir = self.ctrl / "repo"
        self.log = logging.getLogger("devrig")
        self.state = None
        self.adb = None
        self.session = None
        self.vscode = None
        self.android_py = None
        self.vscode_started = False
        self.rig_checked = False
        self._run_t0 = time.monotonic()
        self._typed_at_start = 0
        self.cfg = None
        self.pacing = {"wpm": args.wpm, "pause_min": args.pause_min, "pause_max": args.pause_max,
                       "pause_mean_interval": args.pause_mean_interval, "hold_ms": args.hold_ms}

    # ------------------------------------------------------------- utility --
    def sleep(self, secs):
        time.sleep(max(0.0, secs * self.scale))

    def _setup_logging(self):
        self.ctrl.mkdir(parents=True, exist_ok=True)
        self.log.setLevel(logging.DEBUG)
        self.log.handlers[:] = []
        fh = logging.FileHandler(str(self.ctrl / "devrig.log"), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG if self.args.verbose else logging.INFO)
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        self.log.addHandler(fh)
        self.log.addHandler(ch)

    # --------------------------------------------------------- environment --
    def preflight_local(self):
        a = self.args
        if sys.version_info < (3, 8):
            raise FatalError("Python 3.8+ is required on the host (found %s)" % sys.version.split()[0])
        tools = {"git": a.git_path or shutil.which("git"),
                 "code": a.code_path or shutil.which("code") or shutil.which("code.cmd"),
                 "adb": a.adb_path or shutil.which("adb")}
        missing = [k for k, v in tools.items() if not v]
        if missing:
            raise FatalError("missing required CLI tool(s): %s. Run setup_windows.ps1 -AutoInstall "
                             "or add them to PATH (or pass --git-path/--code-path/--adb-path)."
                             % ", ".join(missing))
        self.tools = tools
        for name, path in tools.items():
            self.log.info("tool %-4s %s", name, path)
        if win_focus.AVAILABLE:
            lid = win_focus.keyboard_layout_id()
            if lid != 0x0409 and not a.allow_non_us_layout:
                raise FatalError("Windows keyboard layout is 0x%04X, but HID scancodes assume US "
                                 "(0x0409). Switch the input language to English (US) or pass "
                                 "--allow-non-us-layout." % lid)
        if self.ws:
            if self.ws == self.ws.parent:
                raise FatalError("workspace must not be a drive root")
            ad = Path(a.archive_dir).resolve() if a.archive_dir else self.ws.parent
            if ad == self.ws or self.ws in ad.parents:
                raise FatalError("--archive-dir must be outside the workspace")
            if self.ctrl == self.ws or self.ws in self.ctrl.parents:
                raise FatalError("--state-dir must be outside the workspace")
        self.adb = adb_bridge.Adb(tools["adb"], a.serial, self.log)
        self.vscode = vscode_env.VSCode(tools["code"], self.ctrl / "vscode-user",
                                        self.ws or Path.cwd(), self.log) if self.ws else None

    # --------------------------------------------------------------- state --
    def load_state(self):
        sp = self.ctrl / "state_manager.json"
        self.state = state_manager.StateManager(sp, self.log)
        if self.args.reset and self.state.exists():
            self.log.warning("--reset: discarding previous ledger and clone cache")
            for p in (sp, Path(str(sp) + ".bak")):
                if p.exists():
                    p.unlink()
            if self.repo_dir.exists():
                shutil.rmtree(str(self.repo_dir), ignore_errors=True)
        if self.state.load():
            self.state.check_identity(self.args.repo_url, self.ws)
            self.state.bump_run()
            self.log.info("resuming run #%d from phase '%s'", self.state.data["run_count"],
                          self.state.phase)
        else:
            self.state.init_new(self.args.repo_url, self.ws, self.args.branch)
            if self.ws.exists() and any(self.ws.iterdir()) and not self.args.allow_nonempty:
                raise FatalError("workspace %s is not empty; use an empty directory or pass "
                                 "--allow-nonempty" % self.ws)
        self.ws.mkdir(parents=True, exist_ok=True)
        if self.state.data.get("config"):
            self.cfg = self.state.data["config"]
        else:
            self.cfg = {"copy_globs": repo_planner.DEFAULT_COPY_GLOBS + list(self.args.copy_glob),
                        "max_file_bytes": self.args.max_file_bytes,
                        "non_ascii": self.args.non_ascii,
                        "include": list(self.args.include), "exclude": list(self.args.exclude)}
            self.state.data["config"] = self.cfg
            self.state.save()

    def ensure_plan(self):
        st = self.state
        clone_url, url_branch = repo_planner.normalize_repo_url(self.args.repo_url)
        branch = self.args.branch or url_branch
        commit = repo_planner.ensure_clone(self.tools["git"], clone_url, branch, self.repo_dir,
                                           st.data["identity"].get("commit"), self.log)
        if st.data["identity"].get("commit") != commit:
            st.set_commit(commit)
        if st.has_plan():
            return
        st.set_phase("cloned")
        files = repo_planner.list_tracked_files(self.tools["git"], str(self.repo_dir))
        self.log.info("repository has %d tracked files; building plan ...", len(files))
        entries = repo_planner.build_plan(str(self.repo_dir), files, self.cfg)
        if not entries:
            raise FatalError("the repository has no files to reproduce")
        st.set_plan(entries)
        st.set_phase("planned")

    def print_plan(self):
        ents = self.state.entries()
        by = {}
        for e in ents:
            by.setdefault(e["mode"], []).append(e)
        typed = sum(e["size"] for e in by.get("type", []))
        self.log.info("plan: %d files | %d typed (%d bytes) | %d copied | %d empty | %d skipped",
                      len(ents), len(by.get("type", [])), typed, len(by.get("copy", [])),
                      len(by.get("empty", [])), len(by.get("skip", [])))
        self.log.info("estimated typing time at %s WPM (pauses included): %s",
                      self.args.wpm, fmt_dur(repo_planner.estimate_seconds(ents, self.args.wpm)))
        for e in ents[:60]:
            self.log.info("  %-5s %9d B  %s%s", e["mode"], e["size"], e["path"],
                          ("   [%s]" % e["note"]) if e["note"] else "")
        if len(ents) > 60:
            self.log.info("  ... %d more", len(ents) - 60)

    # ----------------------------------------------------------------- rig --
    def connect_rig(self):
        a = self.adb
        a.start_server()
        a.select_device()
        if not self.rig_checked:
            if not a.has_root():
                raise FatalError("root (su) is not available on the Android board; the HID gadget "
                                 "and /dev/hidg0 require root")
            found = a.detect_python(self.args.android_python)
            if not found:
                raise FatalError("no Python 3 found on the board. Install Termux + python, or pass "
                                 "--android-python <path>")
            self.android_py = found
            self.log.info("android python: %s", found[0])
            self.rig_checked = True
        self._push_files()
        a.su("pkill -f 'hid_type[r].py'; true", check=False)
        self._ensure_gadget()
        self._start_session()

    def _push_files(self):
        a = self.adb
        a.su("mkdir -p %s && chmod 777 %s" % (REMOTE_DIR, REMOTE_DIR))
        for name in ("hid_typer.py", "setup_hid_gadget.sh"):
            a.push(ROOT / "android" / name, "%s/%s" % (REMOTE_DIR, name))
        a.su("chmod 755 %s/hid_typer.py %s/setup_hid_gadget.sh" % (REMOTE_DIR, REMOTE_DIR))

    def _gadget_state(self):
        hid = self.args.hid_device
        out = self.adb.su("ls %s 2>&1; cat /sys/class/udc/*/state 2>&1; true" % hid,
                          check=False).out
        lines = [l.strip() for l in out.splitlines()]
        return hid in lines, "configured" in lines

    def _run_gadget_setup(self):
        a = self.adb
        self.log.info("configuring USB composite gadget (ADB + HID) - the USB link will re-enumerate")
        a.su("sh %s/setup_hid_gadget.sh" % REMOTE_DIR, timeout=30)
        result, text = None, ""
        end = time.monotonic() + 120
        while time.monotonic() < end:
            time.sleep(2.0)
            try:
                text = a.read_remote_text("%s/gadget_setup.log" % REMOTE_DIR)
            except adb_bridge.AdbError:
                continue                                  # link is down while re-binding
            for line in text.splitlines():
                if line.startswith("DEVRIG_RESULT="):
                    result = line
            if result:
                break
        if not result:
            raise FatalError("gadget setup did not finish in time. Log:\n%s" % text)
        if "FAIL" in result:
            raise FatalError("gadget setup failed: %s\n%s" % (result, text))
        self.log.info("gadget setup: %s", result)
        a.wait_for_device(timeout=90)
        time.sleep(self.args.hid_settle * self.scale)

    def _ensure_gadget(self):
        node_ok, configured = self._gadget_state()
        if node_ok and configured and not self.args.force_gadget_setup:
            self.log.info("HID gadget already active (%s, UDC configured)", self.args.hid_device)
            return
        if not node_ok or self.args.force_gadget_setup:
            self._run_gadget_setup()
            self.args.force_gadget_setup = False
        end = time.monotonic() + 45 * max(self.scale, 0.2)
        while True:
            node_ok, configured = self._gadget_state()
            if node_ok and configured:
                self.log.info("HID gadget enumerated by the workstation")
                return
            if time.monotonic() > end:
                break
            time.sleep(1.5)
        if not node_ok:
            raise FatalError("%s did not appear after gadget setup (see %s/gadget_setup.log on the "
                             "board; kernel may lack CONFIG_USB_CONFIGFS_F_HID)"
                             % (self.args.hid_device, REMOTE_DIR))
        raise FatalError("the HID gadget exists but the workstation has not configured it. Check "
                         "that the cable is a data cable plugged into the Windows host, that the "
                         "board is in USB-device (peripheral) mode, then re-run.")

    def _start_session(self):
        if self.session:
            self.session.close()
        py, env = self.android_py
        for attempt in range(2):
            self.session = typer_session.TyperSession(
                self.adb, py, env, REMOTE_DIR, self.args.hid_device, self.pacing,
                extra_args=self.args.typer_extra, logger=self.log)
            try:
                ev = self.session.start()
                break
            except typer_session.DaemonError as exc:
                if exc.code == "another_instance" and attempt == 0:
                    self.adb.su("pkill -9 -f 'hid_type[r].py'; true", check=False)
                    time.sleep(2)
                    continue
                raise FatalError("typing daemon refused to start: %s" % exc)
        self.log.info("typing daemon ready (v%s, python %s)", ev.get("version"), ev.get("python"))
        self.state.set_rig(serial=self.adb.serial, python=py, hid_device=self.args.hid_device,
                           daemon_version=ev.get("version"))

    def close_session(self):
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None

    # -------------------------------------------------------------- vscode --
    def ensure_vscode(self):
        if self.vscode_started and (not win_focus.AVAILABLE or win_focus.find_vscode_windows()):
            return
        self.log.info("launching isolated VS Code instance (extensions disabled, literal-input settings)")
        self.vscode.launch()
        if win_focus.AVAILABLE:
            end = time.monotonic() + 60
            while time.monotonic() < end and not win_focus.find_vscode_windows():
                time.sleep(1.0)
            if not win_focus.find_vscode_windows():
                raise FatalError("VS Code window did not appear")
        self.sleep(self.args.vscode_startup)
        self.vscode_started = True

    def regain_focus(self, hint=None):
        if not win_focus.AVAILABLE:
            return
        end = time.monotonic() + self.args.focus_timeout
        warned = False
        while time.monotonic() < end:
            if win_focus.foreground_is_vscode(hint):
                return
            win_focus.focus_vscode()
            if not warned:
                self.log.warning("VS Code is not the foreground window; trying to restore focus "
                                 "(keystrokes are held back)")
                warned = True
            time.sleep(0.6)
        raise FatalError("could not bring VS Code to the foreground within %ss" % self.args.focus_timeout)

    def open_in_vscode(self, dest, prefix):
        line = prefix.count(b"\n") + 1
        col = len(prefix) - (prefix.rfind(b"\n") + 1) + 1
        err = None
        for _ in range(3):
            try:
                self.vscode.open_file(str(dest), line, col)
                err = None
                break
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                err = exc
                self.log.warning("VS Code CLI error: %s", exc)
                time.sleep(2.0)
        if err:
            raise FatalError("VS Code CLI keeps failing: %s" % err)
        self.sleep(self.args.settle)
        self.regain_focus(dest.name)

    # ------------------------------------------------------------ file ops --
    def _local(self, rel):
        return self.ws.joinpath(*rel.split("/"))

    def _ensure_dir(self, d):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            self.log.info("mkdir %s", d.relative_to(self.ws).as_posix() or ".")
        rel = d.relative_to(self.ws).as_posix()
        if rel != ".":
            self.state.add_directory(rel)

    def _stable_read(self, dest, timeout=12.0):
        stable = max(0.15, 1.6 * self.scale)
        end = time.monotonic() + timeout
        last, since = "unset", time.monotonic()
        while True:
            try:
                st = dest.stat()
                sig = (st.st_size, st.st_mtime_ns)
            except FileNotFoundError:
                sig = None
            now = time.monotonic()
            if sig != last:
                last, since = sig, now
            elif now - since >= stable or now > end:
                break
            if now > end:
                break
            time.sleep(0.1)
        try:
            return dest.read_bytes()
        except FileNotFoundError:
            return b""

    def _wait_disk_match(self, dest, exp, timeout=20.0):
        end = time.monotonic() + timeout * max(self.scale, 0.25)
        while True:
            try:
                if dest.read_bytes() == exp:
                    return True
            except (FileNotFoundError, PermissionError):
                pass
            if time.monotonic() > end:
                return False
            time.sleep(0.2)

    def _verify_completed(self):
        bad = []
        for e in self.state.entries():
            if e["status"] != "done" or e["mode"] == "skip":
                continue
            p = self._local(e["path"])
            ok = False
            try:
                ok = p.is_file() and p.stat().st_size == e["size"] and \
                    repo_planner.sha256_file(str(p)) == e["sha256"]
            except OSError:
                ok = False
            if not ok:
                bad.append(e["path"])
                self.log.warning("integrity check failed for %s - scheduling re-creation", e["path"])
                self.state.update_entry(e["path"], status="pending", offset=0)
        return bad

    def _log_progress(self, entry):
        s = self.state.stats()
        elapsed = time.monotonic() - self._run_t0
        typed_run = s["typed_bytes_done"] - self._typed_at_start
        eta = ""
        if typed_run > 200 and elapsed > 10:
            rate = typed_run / elapsed
            eta = " | ETA %s" % fmt_dur((s["typed_bytes_total"] - s["typed_bytes_done"]) / rate)
        self.log.info("[%d/%d files | %d/%d typed bytes%s] done: %s", s["files_done"], s["files_total"],
                      s["typed_bytes_done"], s["typed_bytes_total"], eta, entry["path"])

    def process_entry(self, path):
        entry = self.state.entry(path)
        dest = self._local(path)
        if entry["mode"] == "type":
            self.type_file(entry)
            return
        self.state.begin_file(path)
        self._ensure_dir(dest.parent)
        if entry["mode"] == "empty":
            dest.write_bytes(b"")
        else:
            tmp = dest.with_name(dest.name + ".devrig-part")
            shutil.copy2(str(self.repo_dir.joinpath(*path.split("/"))), str(tmp))
            os.replace(str(tmp), str(dest))
            if repo_planner.sha256_file(str(dest)) != entry["sha256"]:
                raise FatalError("copy verification failed for %s" % path)
        self.state.update_entry(path, status="done", offset=entry["size"])
        self.log.info("%s %s%s", "created empty" if entry["mode"] == "empty" else "copied",
                      path, (" (%s)" % entry["note"]) if entry["note"] else "")
        self._log_progress(entry)

    # -------------------------------------------------------------- typing --
    def _keys(self, seq, timeout=30):
        ev = self.session.request({"cmd": "keys", "seq": seq}, timeout=timeout)
        if ev.get("event") == "error":
            self._daemon_error(ev)
        return ev

    def _daemon_error(self, ev):
        code = ev.get("code")
        if code in ("hid_write_failed", "another_instance"):
            raise Recoverable("HID link failure: %s" % ev.get("detail"))
        raise FatalError("typing daemon error %s: %s" % (code, ev.get("detail")))

    def _run_type(self, entry, text, start, prev_char, pre):
        path = entry["path"]
        last = [0.0]

        def abort_check():
            if not win_focus.AVAILABLE:
                return False
            now = time.monotonic()
            if now - last[0] < 1.0:
                return False
            last[0] = now
            return not win_focus.foreground_is_vscode()

        def on_event(ev):
            k = ev.get("event")
            if k == "progress":
                self.state.note_progress(path, ev.get("offset", 0))
            elif k == "pause":
                self.log.debug("operator pause %.1fs at offset %s", ev.get("seconds", 0), ev.get("offset"))
            elif k == "hid_wait":
                self.log.warning("HID write stalled (%s); waiting for the workstation", ev.get("detail"))
            elif k == "warning":
                self.log.warning("daemon: %s", ev.get("msg"))

        return self.session.request({"cmd": "type", "text": text, "start_offset": start,
                                     "prev_char": prev_char, "pre": pre},
                                    abort_check=abort_check, on_event=on_event)

    def type_file(self, entry):
        path = entry["path"]
        dest = self._local(path)
        exp = repo_planner.final_content(str(self.repo_dir), entry, self.cfg)
        text = exp.decode("ascii")
        attempts = 0
        focus_losses = 0
        self.state.begin_file(path)
        while True:
            self._ensure_dir(dest.parent)
            if not dest.exists():
                dest.write_bytes(b"")                      # stub so the IDE can open it
            disk = self._stable_read(dest)
            if disk == exp:
                self.log.info("%s already complete on disk", path)
                self._finish_file(entry, exp, close_tab=False)
                return
            n = common_prefix_len(disk, exp)
            wipe = n != len(disk)                          # disk diverges from the plan
            prefix = 0 if wipe else n
            if wipe:
                self.log.warning("%s: on-disk content diverges at byte %d; retyping from scratch "
                                 "inside the IDE", path, n)
            elif prefix:
                self.log.info("%s: resuming at byte %d of %d (verified on disk)", path, prefix, len(exp))
            else:
                self.log.info("typing %s (%d bytes)", path, len(exp))
            self.state.update_entry(path, offset=prefix, attempts=attempts)
            self.open_in_vscode(dest, b"" if wipe else exp[:prefix])
            pre = [["ctrl", "1"], ["ctrl", "End"]]
            if wipe:
                pre += [["ctrl", "a"], ["delete"]]
            ev = self._run_type(entry, text[prefix:], prefix, text[prefix - 1] if prefix else None, pre)
            kind = ev.get("event")
            if kind == "aborted":
                focus_losses += 1
                self.log.warning("typing halted at offset %s because VS Code lost focus (%d)",
                                 ev.get("offset"), focus_losses)
                if focus_losses > 25:
                    raise FatalError("VS Code keeps losing focus; aborting")
                self.regain_focus()
                continue
            if kind == "error":
                self._daemon_error(ev)
            self._keys([["ctrl", "s"]])
            if self._wait_disk_match(dest, exp):
                self._finish_file(entry, exp, close_tab=True)
                return
            attempts += 1
            self.log.warning("%s: verification failed after typing (attempt %d/%d)", path, attempts,
                             self.args.max_file_attempts)
            if attempts >= self.args.max_file_attempts:
                raise FatalError("%s could not be reproduced exactly after %d attempts; see %s"
                                 % (path, attempts, self.ctrl / "devrig.log"))

    def _finish_file(self, entry, exp, close_tab):
        if close_tab and self.session:
            self._keys([["ctrl", "w"]])
        self.state.update_entry(entry["path"], status="done", offset=len(exp))
        self._log_progress(entry)
        self.sleep(random.uniform(self.args.file_gap_min, self.args.file_gap_max))

    # --------------------------------------------------------------- phases --
    def typing_phase(self):
        self.state.set_phase("typing")
        self._verify_completed()
        pending = self.state.pending_paths()
        self.log.info("%d file(s) remaining", len(pending))
        self._typed_at_start = self.state.stats()["typed_bytes_done"]
        for p in pending:
            self.process_entry(p)

    def audit_phase(self):
        self.state.set_phase("audit")
        bad = self._verify_completed()
        if not bad:
            self.log.info("audit passed: every file matches the plan byte-for-byte")
            return True
        self.log.warning("audit: %d file(s) failed verification and were re-queued", len(bad))
        return False

    def release_workspace(self):
        s = self.session
        if s and s.alive.is_set() and (not win_focus.AVAILABLE or win_focus.foreground_is_vscode()):
            try:
                self._keys([["ctrl", "k"], ["ctrl", "w"]])                 # close all editors
                if not self.args.keep_vscode_open:
                    self._keys([["ctrl", "shift", "w"]])                    # close the window
            except (typer_session.SessionDead, FatalError, Recoverable) as exc:
                self.log.warning("could not close VS Code through the rig: %s", exc)
        self.sleep(2.0)

    def package_phase(self):
        self.state.set_phase("packaging")
        self.release_workspace()
        last = None
        for attempt in range(1, 4):
            try:
                info = packager.create_archive(
                    str(self.ws), self.args.archive_dir, self.args.archive_name,
                    exclude=self.args.archive_exclude, log=self.log,
                    lock_timeout=60 * max(self.scale, 0.1))
                self.state.set_package(info)
                self.state.set_phase("done")
                return info
            except packager.PackagingError as exc:
                last = exc
                self.log.warning("packaging attempt %d failed: %s", attempt, exc)
                time.sleep(3.0 * self.scale)
        raise FatalError("packaging failed: %s" % last)

    # ------------------------------------------------------------ main loop --
    def run_pipeline(self):
        failures = 0
        progress_mark = len(self.state.data["completed_files"])
        while True:
            try:
                if self.state.pending_paths() or self.args.force_rig:
                    self.connect_rig()
                    self.ensure_vscode()
                    self.typing_phase()
                for _ in range(3):
                    if self.audit_phase():
                        break
                    if not self.session:
                        self.connect_rig()
                        self.ensure_vscode()
                    self.typing_phase()
                else:
                    raise FatalError("audit keeps failing; see the log for the offending files")
                if self.args.no_package:
                    self.state.set_phase("done")
                    self.log.info("packaging skipped (--no-package)")
                    return None
                return self.package_phase()
            except (Recoverable, adb_bridge.AdbDisconnected, typer_session.SessionDead,
                    adb_bridge.AdbError) as exc:
                if isinstance(exc, adb_bridge.AdbUnauthorized):
                    raise FatalError(str(exc))
                done = len(self.state.data["completed_files"])
                if done > progress_mark:
                    failures, progress_mark = 0, done
                failures += 1
                self.state.event("recoverable failure: %s" % exc)
                self.state.flush()
                self.log.error("rig failure (%d/%d): %s", failures, self.args.max_reconnects, exc)
                self.close_session()
                if failures > self.args.max_reconnects:
                    raise FatalError("too many consecutive rig failures; state saved, re-run to resume")
                delay = min(2 ** min(failures, 5), 30)
                self.log.info("reconnecting in %ds ...", delay)
                time.sleep(delay * max(self.scale, 0.1))
                try:
                    self.adb.wait_for_device(timeout=self.args.reconnect_timeout)
                except adb_bridge.AdbUnauthorized as e2:
                    raise FatalError(str(e2))
                except adb_bridge.AdbError as e2:
                    self.log.error("%s", e2)

    # ------------------------------------------------------------- commands --
    def cmd_status(self):
        sm = state_manager.StateManager(self.ctrl / "state_manager.json", self.log)
        if not sm.load():
            print("no ledger at %s" % (self.ctrl / "state_manager.json"))
            return 1
        s = sm.stats()
        d = sm.data
        print("run id        : %s (run #%d)" % (d["created_at"], d["run_count"]))
        print("repo          : %s @ %s" % (d["identity"]["repo_url"], (d["identity"]["commit"] or "?")[:10]))
        print("workspace     : %s" % d["identity"]["workspace"])
        print("phase         : %s" % d["phase"])
        print("files         : %d/%d done" % (s["files_done"], s["files_total"]))
        print("typed bytes   : %d/%d" % (s["typed_bytes_done"], s["typed_bytes_total"]))
        print("current file  : %s" % d.get("current_file"))
        if d.get("package"):
            print("archive       : %s" % d["package"]["archive"])
        for h in d.get("history", [])[-5:]:
            print("  %s  %s" % (h["t"], h["msg"]))
        return 0

    def cmd_teardown(self):
        self.adb.start_server()
        self.adb.select_device()
        if not self.adb.has_root():
            raise FatalError("root is required to tear down the gadget")
        self._push_files()
        self.adb.su("pkill -f 'hid_type[r].py'; true", check=False)
        self.adb.su("sh %s/setup_hid_gadget.sh --teardown" % REMOTE_DIR, timeout=30)
        for _ in range(30):
            time.sleep(2)
            try:
                text = self.adb.read_remote_text("%s/gadget_setup.log" % REMOTE_DIR)
            except adb_bridge.AdbError:
                continue
            if "DEVRIG_RESULT=" in text:
                print(text.strip())
                return 0 if "FAIL" not in text else 1
        print("teardown did not report a result")
        return 1

    def cmd_check(self):
        self.connect_rig()
        self.log.info("rig check passed: root OK, python OK, HID gadget enumerated, daemon alive")
        ev = self.session.request({"cmd": "status"}, timeout=10)
        self.log.info("daemon status: %s", {k: v for k, v in ev.items() if k not in ("event", "id")})
        self.close_session()
        return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="orchestrator.py",
        description="DevRig-HID: type a GitHub repository into VS Code through a physical USB "
                    "HID keyboard (Android board), then package the result.")
    p.add_argument("repo_url", nargs="?", help="GitHub repository URL")
    p.add_argument("workspace", nargs="?", help="local workspace directory to build in")
    p.add_argument("--branch")
    g = p.add_argument_group("tools and rig")
    g.add_argument("--serial", help="adb serial (needed when several devices are attached)")
    g.add_argument("--adb-path")
    g.add_argument("--code-path")
    g.add_argument("--git-path")
    g.add_argument("--android-python", help="python3 path on the board")
    g.add_argument("--hid-device", default="/dev/hidg0")
    g.add_argument("--hid-settle", type=float, default=8.0, help="seconds to let Windows enumerate the keyboard")
    g.add_argument("--force-gadget-setup", action="store_true")
    g.add_argument("--allow-non-us-layout", action="store_true")
    g.add_argument("--typer-extra", default="", help="extra arguments for hid_typer.py serve")
    g = p.add_argument_group("typing behaviour")
    g.add_argument("--wpm", type=float, default=70.0, help="baseline speed (default 70)")
    g.add_argument("--pause-min", type=float, default=3.0)
    g.add_argument("--pause-max", type=float, default=7.0)
    g.add_argument("--pause-mean-interval", type=float, default=150.0,
                   help="mean seconds between Poisson distraction pauses (0 disables)")
    g.add_argument("--hold-ms", type=float, default=68.0)
    g.add_argument("--file-gap-min", type=float, default=1.0)
    g.add_argument("--file-gap-max", type=float, default=3.5)
    g = p.add_argument_group("plan")
    g.add_argument("--non-ascii", choices=("copy", "transliterate", "error"), default="copy",
                   help="files with characters lacking a US scancode (default: copy verbatim)")
    g.add_argument("--max-file-bytes", type=int, default=1 << 20)
    g.add_argument("--copy-glob", action="append", default=[])
    g.add_argument("--include", action="append", default=[])
    g.add_argument("--exclude", action="append", default=[])
    g = p.add_argument_group("packaging")
    g.add_argument("--no-package", action="store_true")
    g.add_argument("--archive-dir")
    g.add_argument("--archive-name")
    g.add_argument("--archive-exclude", action="append", default=[])
    g.add_argument("--keep-vscode-open", action="store_true")
    g = p.add_argument_group("run control")
    g.add_argument("--state-dir")
    g.add_argument("--reset", action="store_true", help="discard the ledger and start over")
    g.add_argument("--allow-nonempty", action="store_true")
    g.add_argument("--dry-run", action="store_true", help="clone and print the plan only")
    g.add_argument("--check-only", action="store_true", help="verify tools, root, gadget and daemon, then exit")
    g.add_argument("--status", action="store_true", help="print the ledger summary and exit")
    g.add_argument("--teardown-gadget", action="store_true")
    g.add_argument("--max-reconnects", type=int, default=40)
    g.add_argument("--reconnect-timeout", type=float, default=300.0)
    g.add_argument("--max-file-attempts", type=int, default=3)
    g.add_argument("--focus-timeout", type=float, default=300.0)
    g.add_argument("--vscode-startup", type=float, default=4.0)
    g.add_argument("--settle", type=float, default=1.5, help="seconds after opening a file before typing")
    g.add_argument("--timing-scale", type=float, default=1.0, help=argparse.SUPPRESS)
    g.add_argument("--force-rig", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("-v", "--verbose", action="store_true")
    g.add_argument("--version", action="version", version="DevRig-HID " + VERSION)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    needs_run = not (args.teardown_gadget or args.check_only)
    if needs_run and not (args.repo_url and args.workspace):
        print("error: repo_url and workspace are required", file=sys.stderr)
        return 2
    orch = Orchestrator(args)
    orch._setup_logging()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        if args.status:
            return orch.cmd_status()
        orch.preflight_local()
        if args.teardown_gadget:
            return orch.cmd_teardown()
        if args.check_only:
            return orch.cmd_check()
        orch.ctrl.mkdir(parents=True, exist_ok=True)
        with state_manager.ProcessLock(orch.ctrl / "devrig.lock"):
            orch.load_state()
            orch.ensure_plan()
            orch.print_plan()
            if args.dry_run:
                return 0
            if orch.state.phase == "done" and orch.state.data.get("package") and \
                    os.path.exists(orch.state.data["package"]["archive"]) and not args.no_package:
                orch.log.info("this run is already complete: %s", orch.state.data["package"]["archive"])
                return 0
            info = orch.run_pipeline()
            orch.close_session()
            if info:
                orch.log.info("DONE  archive: %s", info["archive"])
                orch.log.info("      sha256 : %s", info["sha256"])
            else:
                orch.log.info("DONE")
            return 0
    except state_manager.LockError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except (FatalError, state_manager.StateCorrupt, state_manager.StateMismatch,
            repo_planner.PlanError, adb_bridge.AdbError) as exc:
        orch.log.error("FATAL: %s", exc)
        if orch.state and orch.state.data:
            orch.state.event("fatal: %s" % exc)
            orch.state.flush()
        return 1
    except KeyboardInterrupt:
        orch.log.warning("interrupted - state saved; re-run the same command to resume")
        if orch.state and orch.state.data:
            orch.state.flush()
        return 130
    finally:
        orch.close_session()


if __name__ == "__main__":
    sys.exit(main())
