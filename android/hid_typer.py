#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: android/hid_typer.py
==================================

Hardware keystroke daemon.  Runs as root on the Android development board and
writes raw 8-byte boot-keyboard HID reports to the USB gadget node (/dev/hidg0)
so that the connected workstation sees a genuine USB keyboard.

Sub-commands
------------
  serve     JSON-lines session driven by the host orchestrator (stdin/stdout)
  selftest  Validate scancode tables, report framing and pacing statistics
  demo      Type a short string (calibration / smoke test)

Only the Python standard library is used (Python >= 3.6).

Wire protocol (serve mode) -- one JSON object per line
------------------------------------------------------
host -> daemon
  {"cmd":"ping"}                                  watchdog heartbeat
  {"cmd":"hello","id":N}
  {"cmd":"configure","id":N,"wpm":70,...}          replace pacing parameters
  {"cmd":"keys","id":N,"seq":[["ctrl","s"],...]}   tap key combinations
  {"cmd":"type","id":N,"text":"...","start_offset":0,"prev_char":"\\n",
   "pre":[["ctrl","End"]]}                         type text (ASCII subset)
  {"cmd":"abort","id":N}                           stop command N immediately
  {"cmd":"shutdown"}
daemon -> host
  {"event":"ready"|"ack"|"progress"|"typed"|"aborted"|"error"|"pause"|
           "hid_wait"|"hb"|"log"|"warning", ...}
"""
from __future__ import print_function

import argparse
import errno
import json
import math
import os
import queue
import random
import sys
import threading
import time
import traceback

VERSION = "1.0.0"
REPORT_LEN = 8
ZERO_REPORT = bytes(REPORT_LEN)

# --------------------------------------------------------------------------
# HID boot keyboard report descriptor (63 bytes) -- identical to the one that
# android/setup_hid_gadget.sh programs into ConfigFS.
# --------------------------------------------------------------------------
HID_REPORT_DESCRIPTOR = bytes(bytearray([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01,
    0x75, 0x08, 0x81, 0x03, 0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01,
    0x29, 0x05, 0x91, 0x02, 0x95, 0x01, 0x75, 0x03, 0x91, 0x03, 0x95, 0x06,
    0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65,
    0x81, 0x00, 0xC0,
]))

# --------------------------------------------------------------------------
# Scancode tables (USB HID usage page 0x07, US layout)
# --------------------------------------------------------------------------
MODIFIER_BITS = {
    "lctrl": 0x01, "lshift": 0x02, "lalt": 0x04, "lgui": 0x08,
    "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rgui": 0x80,
}
MODIFIER_ALIASES = {
    "ctrl": "lctrl", "control": "lctrl", "shift": "lshift", "alt": "lalt",
    "gui": "lgui", "win": "lgui", "super": "lgui", "meta": "lgui",
    "rightctrl": "rctrl", "rightshift": "rshift", "rightalt": "ralt",
    "altgr": "ralt",
}
LSHIFT = MODIFIER_BITS["lshift"]


def _build_keycodes():
    codes = {}
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        codes[c] = 0x04 + i
    for i, c in enumerate("1234567890"):
        codes[c] = 0x1E + i
    codes.update({
        "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29,
        "backspace": 0x2A, "tab": 0x2B, "space": 0x2C, "minus": 0x2D,
        "equal": 0x2E, "lbracket": 0x2F, "rbracket": 0x30, "backslash": 0x31,
        "semicolon": 0x33, "quote": 0x34, "grave": 0x35, "comma": 0x36,
        "period": 0x37, "slash": 0x38, "capslock": 0x39,
        "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48,
        "insert": 0x49, "home": 0x4A, "pageup": 0x4B, "delete": 0x4C,
        "del": 0x4C, "end": 0x4D, "pagedown": 0x4E, "right": 0x4F,
        "left": 0x50, "down": 0x51, "up": 0x52, "numlock": 0x53,
        "menu": 0x65,
    })
    for n in range(1, 13):
        codes["f%d" % n] = 0x3A + n - 1
    return codes


KEYCODES = _build_keycodes()

_UNSHIFTED = {
    " ": 0x2C, "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
    "\n": 0x28, "\t": 0x2B,
}
_SHIFTED = {
    "!": 0x1E, "@": 0x1F, "#": 0x20, "$": 0x21, "%": 0x22, "^": 0x23,
    "&": 0x24, "*": 0x25, "(": 0x26, ")": 0x27, "_": 0x2D, "+": 0x2E,
    "{": 0x2F, "}": 0x30, "|": 0x31, ":": 0x33, '"': 0x34, "~": 0x35,
    "<": 0x36, ">": 0x37, "?": 0x38,
}
SHIFT_CHARS = frozenset(_SHIFTED)


def _build_char_map():
    cmap = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        cmap[c] = (0, KEYCODES[c])
        cmap[c.upper()] = (LSHIFT, KEYCODES[c])
    for c in "1234567890":
        cmap[c] = (0, KEYCODES[c])
    for c, code in _UNSHIFTED.items():
        cmap[c] = (0, code)
    for c, code in _SHIFTED.items():
        cmap[c] = (LSHIFT, code)
    return cmap


# char -> (modifier_byte, keycode)
CHAR_MAP = _build_char_map()
# (modifier_byte, keycode) -> char   (used by selftest / simulators)
REVERSE_MAP = dict(((m, k), c) for c, (m, k) in CHAR_MAP.items())


def resolve_key(token):
    """Return (implicit_modifier, keycode) for a key name or single char."""
    low = token.lower()
    if low in KEYCODES:
        return 0, KEYCODES[low]
    if len(token) == 1 and token in CHAR_MAP:
        return CHAR_MAP[token]
    raise KeyError("unknown key name: %r" % (token,))


def parse_combo(names):
    """['ctrl','shift','p'] -> ([0x01, 0x02], 0x13)."""
    mods = []
    key = None
    for name in names:
        alias = MODIFIER_ALIASES.get(name.lower(), name.lower())
        if alias in MODIFIER_BITS:
            mods.append(MODIFIER_BITS[alias])
            continue
        if key is not None:
            raise ValueError("combo has more than one non-modifier key: %r" % (names,))
        implicit, key = resolve_key(name)
        if implicit and implicit not in mods:
            mods.append(implicit)
    return mods, (key or 0)


def build_report(modifier, keys=()):
    """Build a standard 8-byte boot keyboard report."""
    keys = list(keys)[:6]
    return bytes(bytearray([modifier & 0xFF, 0] + keys + [0] * (6 - len(keys))))


def decode_report(report):
    """Inverse of build_report -> (modifier, [keycodes])."""
    r = bytearray(report)
    return r[0], [k for k in r[2:8] if k]


def untypeable(text):
    """Return [(index, char)] for characters without a scancode mapping."""
    return [(i, c) for i, c in enumerate(text) if c not in CHAR_MAP]


# --------------------------------------------------------------------------
# Pacing: balanced human cadence + Poisson-distributed distraction pauses
# --------------------------------------------------------------------------
class Pacing(object):
    SIGMA = 0.27

    def __init__(self, wpm=70.0, pause_min=3.0, pause_max=7.0,
                 pause_mean_interval=150.0, hold_ms=68.0, hold_sd_ms=18.0,
                 seed=None):
        if wpm <= 0:
            raise ValueError("wpm must be positive")
        if pause_max < pause_min:
            raise ValueError("pause_max < pause_min")
        self.wpm = float(wpm)
        self.pause_min = float(pause_min)
        self.pause_max = float(pause_max)
        self.pause_mean_interval = float(pause_mean_interval)
        self.hold_ms = float(hold_ms)
        self.hold_sd_ms = float(hold_sd_ms)
        if seed is None:
            seed = int(''.join('%02x' % b for b in bytearray(os.urandom(8))), 16)
        self.rng = random.Random(seed)
        self.base = 60.0 / (self.wpm * 5.0)          # seconds per character
        self.lo = max(0.004, self.base * 0.25)
        self.hi = self.base * 7.0
        self._rhythm = 0.0
        self._since_pause = 0.0
        self._next_pause = self._draw_gap()

    # -- inter-key interval ------------------------------------------------
    def char_interval(self, prev, ch, line_start):
        m = 1.0
        if ch == " ":
            m = 0.45 if line_start else 1.12
        elif ch == "\t":
            m = 0.5 if line_start else 1.0
        elif ch == "\n":
            m = 1.25
        elif ch in SHIFT_CHARS or ch.isupper():
            m = 1.25
        elif ch.isdigit():
            m = 1.12
        elif not ch.isalnum():
            m = 1.15
        # AR(1) "rhythm" term gives short fast/slow runs instead of white noise
        self._rhythm = 0.75 * self._rhythm + 0.25 * self.rng.gauss(0.0, 0.35)
        noise = self.rng.lognormvariate(-0.5 * self.SIGMA ** 2, self.SIGMA)
        t = self.base * m * math.exp(self._rhythm) * noise
        if prev == "\n":                               # brief think between lines
            t += self.rng.lognormvariate(math.log(max(0.02, self.base * 2.6)), 0.5)
        return min(self.hi, max(self.lo, t))

    def hold(self):
        lo = max(0.003, self.hold_ms * 0.35 / 1000.0)
        hi = self.hold_ms * 2.4 / 1000.0
        v = self.rng.gauss(self.hold_ms, self.hold_sd_ms) / 1000.0
        return min(hi, max(lo, v))

    def jitter(self, lo, hi):
        scale = min(1.0, self.hold_ms / 68.0)
        return self.rng.uniform(lo * scale, hi * scale)

    # -- Poisson process of distraction pauses -----------------------------
    def _draw_gap(self):
        if self.pause_mean_interval <= 0:
            return float("inf")
        return self.rng.expovariate(1.0 / self.pause_mean_interval)

    def account(self, seconds):
        self._since_pause += seconds

    def pause_due(self):
        return self._since_pause >= self._next_pause

    def draw_pause(self):
        self._since_pause = 0.0
        self._next_pause = self._draw_gap()
        return self.rng.uniform(self.pause_min, self.pause_max)


# --------------------------------------------------------------------------
# Hardware access
# --------------------------------------------------------------------------
class Aborted(Exception):
    pass


class HidWriteError(Exception):
    pass


def _errno_set(*names):
    return set(getattr(errno, n) for n in names if hasattr(errno, n))


class HidDevice(object):
    TRANSIENT = _errno_set("EAGAIN", "EINTR", "ETIMEDOUT", "ESHUTDOWN", "EPIPE",
                           "ENODEV", "ENXIO", "EBADF", "EBUSY", "EIO", "EPROTO",
                           "ECONNRESET", "ENOENT")
    REOPEN = _errno_set("ENODEV", "ENXIO", "EBADF", "ESHUTDOWN", "EPIPE", "ENOENT")

    def __init__(self, path, retry_seconds, emit, should_abort,
                 create=False, leds=True):
        self.path = path
        self.retry_seconds = retry_seconds
        self.emit = emit
        self.should_abort = should_abort
        self.create = create
        self.leds = leds
        self.fd = None
        self.led = None

    def _open(self):
        flags = os.O_RDWR | getattr(os, "O_NONBLOCK", 0)
        if self.create:                       # test mode: regular file target
            flags |= os.O_CREAT | os.O_APPEND
        self.fd = os.open(self.path, flags, 0o666)

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def close(self):
        self._close()

    def write(self, report, budget=None, ignore_abort=False):
        if len(report) != REPORT_LEN:
            raise ValueError("HID report must be exactly 8 bytes")
        budget = self.retry_seconds if budget is None else budget
        deadline = time.monotonic() + budget
        delay = 0.02
        announced = False
        while True:
            try:
                if self.fd is None:
                    self._open()
                n = os.write(self.fd, report)
                if n == REPORT_LEN:
                    return
                raise OSError(errno.EIO, "short write (%d bytes)" % n)
            except OSError as exc:
                if not ignore_abort and self.should_abort():
                    raise Aborted()
                if exc.errno not in self.TRANSIENT or time.monotonic() >= deadline:
                    raise HidWriteError("%s (errno %s)" % (exc.strerror or exc, exc.errno))
                if exc.errno in self.REOPEN:
                    self._close()
                if not announced:
                    announced = True
                    self.emit({"event": "hid_wait", "errno": exc.errno,
                               "detail": exc.strerror or str(exc)})
                time.sleep(delay)
                delay = min(delay * 2, 1.0)

    def drain_leds(self):
        """Read pending LED output reports from the host (Caps/Num/Scroll)."""
        if not self.leds:
            return None
        try:
            if self.fd is None:
                self._open()
        except OSError:
            return None
        last = None
        while True:
            try:
                data = os.read(self.fd, 16)
            except OSError:
                break
            if not data:
                break
            last = bytearray(data)[0]
        if last is not None:
            self.led = last
        return last


# --------------------------------------------------------------------------
# Typist: converts text and key combinations into press/hold/release reports
# --------------------------------------------------------------------------
class Typist(object):
    def __init__(self, dev, pacing, emit, should_abort):
        self.dev = dev
        self.pacing = pacing
        self.emit = emit
        self.should_abort = should_abort
        self.offset = 0

    def sleep(self, secs):
        end = time.monotonic() + secs
        while True:
            if self.should_abort():
                raise Aborted()
            rem = end - time.monotonic()
            if rem <= 0:
                return
            time.sleep(min(rem, 0.05))

    def release_all(self):
        try:
            self.dev.write(ZERO_REPORT, budget=1.5, ignore_abort=True)
        except Exception:
            pass

    # -- single character ---------------------------------------------------
    def press_char(self, ch):
        mod, code = CHAR_MAP[ch]
        pc = self.pacing
        hold = pc.hold()
        if mod:
            self.dev.write(build_report(mod))                 # modifier down
            self.sleep(pc.jitter(0.008, 0.030))
        self.dev.write(build_report(mod, (code,)))             # key down
        self.sleep(hold)
        self.dev.write(build_report(mod))                      # key up
        if mod:
            self.sleep(pc.jitter(0.006, 0.025))
            self.dev.write(ZERO_REPORT)                        # modifier up

    # -- key combination ------------------------------------------------------
    def tap_combo(self, names):
        mods, key = parse_combo(names)
        pc = self.pacing
        cur = 0
        try:
            for m in mods:
                cur |= m
                self.dev.write(build_report(cur))
                self.sleep(pc.jitter(0.015, 0.045))
            if key:
                self.dev.write(build_report(cur, (key,)))
                self.sleep(pc.hold())
                self.dev.write(build_report(cur))
            for m in reversed(mods):
                cur &= ~m
                self.sleep(pc.jitter(0.010, 0.035))
                self.dev.write(build_report(cur))
        except BaseException:
            self.release_all()
            raise

    def run_keys(self, seq):
        for i, combo in enumerate(seq):
            self.tap_combo(combo)
            if i != len(seq) - 1:
                self.sleep(self.pacing.rng.uniform(0.10, 0.30) * min(1.0, self.pacing.hold_ms / 68.0))
        self.sleep(self.pacing.rng.uniform(0.08, 0.22) * min(1.0, self.pacing.hold_ms / 68.0))

    # -- CapsLock guard -------------------------------------------------------
    def ensure_caps_off(self):
        if not self.dev.leds:
            return
        for _ in range(3):
            self.dev.drain_leds()
            if self.dev.led is None or not (self.dev.led & 0x02):
                return
            self.emit({"event": "log", "level": "info",
                       "msg": "CapsLock is active on the workstation; toggling it off"})
            self.tap_combo(["capslock"])
            self.sleep(0.6)
        self.dev.drain_leds()
        if self.dev.led is not None and (self.dev.led & 0x02):
            self.emit({"event": "warning", "code": "caps_stuck",
                       "msg": "CapsLock could not be cleared"})

    # -- bulk text ------------------------------------------------------------
    def type_text(self, cmd_id, text, start_offset=0, prev_char=None):
        pc = self.pacing
        self.offset = start_offset
        prev = prev_char if prev_char is not None else ("\n" if start_offset == 0 else "")
        line_start = prev in ("\n", "")
        last_emit = time.monotonic()
        try:
            for i, ch in enumerate(text):
                if self.should_abort():
                    raise Aborted()
                t0 = time.monotonic()
                self.press_char(ch)
                self.offset = start_offset + i + 1
                interval = pc.char_interval(prev, ch, line_start)
                if ch == "\n":
                    line_start = True
                elif ch not in " \t":
                    line_start = False
                prev = ch
                self.sleep(max(0.004, interval - (time.monotonic() - t0)))
                pc.account(interval)
                if pc.pause_due():
                    secs = pc.draw_pause()
                    self.emit({"event": "pause", "id": cmd_id, "seconds": round(secs, 2),
                               "offset": self.offset})
                    self.sleep(secs)
                now = time.monotonic()
                if (i % 40 == 39) or now - last_emit >= 1.0:
                    last_emit = now
                    self.emit({"event": "progress", "id": cmd_id, "offset": self.offset})
                if i % 500 == 499:
                    self.ensure_caps_off()
        finally:
            self.release_all()
        return self.offset


# --------------------------------------------------------------------------
# Session server
# --------------------------------------------------------------------------
class Server(object):
    def __init__(self, args):
        self.args = args
        self.out_lock = threading.Lock()
        self.abort_lock = threading.Lock()
        self.abort_ids = set()
        self.current_id = None
        self.dead = threading.Event()
        self.cmds = queue.Queue()
        self.last_ping = time.monotonic()
        self.pacing = self._make_pacing({})
        self.dev = HidDevice(args.device, args.hid_retry, self.emit, self.should_abort,
                             create=args.allow_create, leds=not args.no_leds)
        self.typist = Typist(self.dev, self.pacing, self.emit, self.should_abort)
        self._lock_fh = None

    # -- helpers -----------------------------------------------------------
    def _make_pacing(self, over):
        a = self.args
        g = lambda k, d: over.get(k, d)  # noqa: E731
        return Pacing(wpm=g("wpm", a.wpm), pause_min=g("pause_min", a.pause_min),
                      pause_max=g("pause_max", a.pause_max),
                      pause_mean_interval=g("pause_mean_interval", a.pause_mean_interval),
                      hold_ms=g("hold_ms", a.hold_ms), hold_sd_ms=g("hold_sd_ms", a.hold_sd_ms),
                      seed=g("seed", a.seed))

    def log(self, level, msg):
        if self.args.log:
            try:
                with open(self.args.log, "a") as fh:
                    fh.write("%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg))
            except (IOError, OSError):
                pass

    def emit(self, obj):
        line = json.dumps(obj, separators=(",", ":"))
        with self.out_lock:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except (IOError, OSError, ValueError):
                self.dead.set()

    def should_abort(self):
        if self.dead.is_set():
            return True
        if time.monotonic() - self.last_ping > self.args.watchdog:
            self.log("error", "watchdog expired: no heartbeat from host")
            self.dead.set()
            return True
        cid = self.current_id
        if cid is not None:
            with self.abort_lock:
                return cid in self.abort_ids
        return False

    # -- threads ---------------------------------------------------------------
    def _reader(self):
        try:
            for raw in iter(sys.stdin.readline, ""):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    self.emit({"event": "log", "level": "warn", "msg": "bad json from host"})
                    continue
                c = msg.get("cmd")
                if c == "ping":
                    self.last_ping = time.monotonic()
                elif c == "abort":
                    with self.abort_lock:
                        self.abort_ids.add(msg.get("id"))
                else:
                    self.last_ping = time.monotonic()
                    self.cmds.put(msg)
        finally:
            self.cmds.put({"cmd": "shutdown", "reason": "stdin_closed"})
            self.dead.set()                      # host gone: stop typing at once

    def _heartbeat(self):
        while not self.dead.is_set():
            self.emit({"event": "hb", "t": round(time.time(), 1)})
            time.sleep(3.0)

    def _acquire_instance_lock(self):
        if not self.args.lock:
            return True
        try:
            import fcntl
        except ImportError:
            return True
        self._lock_fh = open(self.args.lock, "a")
        end = time.monotonic() + 6.0
        while True:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (IOError, OSError):
                if time.monotonic() > end:
                    return False
                time.sleep(0.3)

    # -- command handling -----------------------------------------------------
    def handle(self, msg):
        cid = msg.get("id")
        cmd = msg.get("cmd")
        self.current_id = cid
        try:
            if cmd == "hello":
                self.emit({"event": "ack", "id": cid, "version": VERSION,
                           "uid": os.geteuid() if hasattr(os, "geteuid") else -1})
            elif cmd == "configure":
                over = dict((k, v) for k, v in msg.items() if k not in ("cmd", "id"))
                self.pacing = self._make_pacing(over)
                self.typist.pacing = self.pacing
                self.emit({"event": "ack", "id": cid})
            elif cmd == "status":
                self.dev.drain_leds()
                self.emit({"event": "ack", "id": cid, "led": self.dev.led,
                           "offset": self.typist.offset})
            elif cmd == "keys":
                self.typist.run_keys(msg.get("seq", []))
                self.emit({"event": "ack", "id": cid})
            elif cmd == "type":
                self._cmd_type(cid, msg)
            else:
                self.emit({"event": "error", "id": cid, "code": "unknown_cmd",
                           "detail": str(cmd)})
        except Aborted:
            self.typist.release_all()
            self.emit({"event": "aborted", "id": cid, "offset": self.typist.offset})
        except HidWriteError as exc:
            self.typist.release_all()
            self.log("error", "HID write failed: %s" % exc)
            self.emit({"event": "error", "id": cid, "code": "hid_write_failed",
                       "detail": str(exc), "offset": self.typist.offset})
        except (KeyError, ValueError) as exc:
            self.emit({"event": "error", "id": cid, "code": "bad_request", "detail": str(exc)})
        except Exception as exc:                                  # pragma: no cover
            self.log("error", traceback.format_exc())
            self.emit({"event": "error", "id": cid, "code": "internal", "detail": repr(exc)})
        finally:
            with self.abort_lock:
                self.abort_ids.discard(cid)
            self.current_id = None

    def _cmd_type(self, cid, msg):
        text = msg.get("text", "")
        start = int(msg.get("start_offset", 0))
        bad = untypeable(text)
        if bad:
            self.emit({"event": "error", "id": cid, "code": "unmapped_char",
                       "detail": "%d unmappable character(s), first at index %d: %r"
                                 % (len(bad), bad[0][0], bad[0][1]),
                       "index": bad[0][0]})
            return
        self.typist.ensure_caps_off()
        pre = msg.get("pre")
        if pre:
            self.typist.run_keys(pre)
        end = self.typist.type_text(cid, text, start, msg.get("prev_char"))
        self.emit({"event": "typed", "id": cid, "offset": end})

    # -- main loop --------------------------------------------------------------
    def run(self):
        if not self._acquire_instance_lock():
            self.emit({"event": "error", "code": "another_instance",
                       "detail": "another hid_typer instance holds the lock"})
            return 5
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        self.emit({"event": "ready", "version": VERSION, "device": self.args.device,
                   "uid": os.geteuid() if hasattr(os, "geteuid") else -1,
                   "python": sys.version.split()[0]})
        code = 0
        try:
            while not self.dead.is_set():
                try:
                    msg = self.cmds.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - self.last_ping > self.args.watchdog:
                        self.log("error", "watchdog expired while idle")
                        code = 4
                        break
                    continue
                if msg.get("cmd") == "shutdown":
                    break
                self.handle(msg)
            if self.dead.is_set() and code == 0:
                code = 3
        finally:
            self.typist.release_all()
            self.dev.close()
        return code


# --------------------------------------------------------------------------
# selftest / demo
# --------------------------------------------------------------------------
def selftest(args):
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + msg)
        ok = ok and cond

    printable = [chr(c) for c in range(32, 127)] + ["\n", "\t"]
    check(all(c in CHAR_MAP for c in printable), "all 95 printable ASCII + LF + TAB are mapped")
    rt = True
    for c in printable:
        m, k = CHAR_MAP[c]
        rep = build_report(m, (k,))
        mm, kk = decode_report(rep)
        rt = rt and len(rep) == 8 and REVERSE_MAP.get((mm, kk[0])) == c
    check(rt, "every character round-trips through an 8-byte report")
    check(len(HID_REPORT_DESCRIPTOR) == 63, "report descriptor is 63 bytes")
    check(build_report(0x02, (0x04,)) == b"\x02\x00\x04\x00\x00\x00\x00\x00", "report framing")
    mods, key = parse_combo(["ctrl", "shift", "p"])
    check(mods == [0x01, 0x02] and key == 0x13, "combo parsing (ctrl+shift+p)")
    check(parse_combo(["ctrl", "End"]) == ([0x01], 0x4D), "combo parsing (ctrl+End)")
    # pacing statistics
    pc = Pacing(wpm=70, seed=7)
    sample = ("def handler(request):\n    value = request.get('key', 0) + 42\n"
              "    return {\"ok\": True, \"v\": value}\n") * 200
    total, prev, ls = 0.0, "\n", True
    for ch in sample:
        t = pc.char_interval(prev, ch, ls)
        total += t
        pc.account(t)
        if ch == "\n":
            ls = True
        elif ch not in " \t":
            ls = False
        prev = ch
    cps = len(sample) / total
    nominal = 70 * 5 / 60.0
    check(0.55 * nominal <= cps <= 1.25 * nominal,
          "effective speed %.2f chars/s (nominal %.2f)" % (cps, nominal))
    pc = Pacing(wpm=70, pause_mean_interval=100, seed=3)
    pauses, lengths = 0, []
    for _ in range(4000):
        pc.account(0.17)
        if pc.pause_due():
            lengths.append(pc.draw_pause())
            pauses += 1
    expect = 4000 * 0.17 / 100.0
    check(pauses > 0 and all(3.0 <= x <= 7.0 for x in lengths) and 0.4 * expect <= pauses <= 2.0 * expect,
          "Poisson pauses: %d in 680s (expected ~%.1f), all within 3-7 s" % (pauses, expect))
    if args.check_device:
        check(os.path.exists(args.device) and os.access(args.device, os.W_OK),
              "%s exists and is writable" % args.device)
    print("SELFTEST " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def demo(args):
    dev = HidDevice(args.device, args.hid_retry, lambda o: None, lambda: False,
                    create=args.allow_create, leds=not args.no_leds)
    pc = Pacing(wpm=args.wpm, pause_min=args.pause_min, pause_max=args.pause_max,
                pause_mean_interval=args.pause_mean_interval, hold_ms=args.hold_ms,
                hold_sd_ms=args.hold_sd_ms, seed=args.seed)
    typ = Typist(dev, pc, lambda o: None, lambda: False)
    bad = untypeable(args.text)
    if bad:
        print("unmappable character at index %d: %r" % bad[0], file=sys.stderr)
        return 2
    print("typing in %.1fs - focus a text field on the workstation now" % args.delay)
    time.sleep(args.delay)
    typ.type_text("demo", args.text)
    dev.close()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="hid_typer.py", description=__doc__.split("\n")[1])
    sub = p.add_subparsers(dest="mode")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--device", default="/dev/hidg0")
    common.add_argument("--wpm", type=float, default=70.0)
    common.add_argument("--pause-min", type=float, default=3.0)
    common.add_argument("--pause-max", type=float, default=7.0)
    common.add_argument("--pause-mean-interval", type=float, default=150.0,
                        help="mean seconds between Poisson distraction pauses (0 disables)")
    common.add_argument("--hold-ms", type=float, default=68.0)
    common.add_argument("--hold-sd-ms", type=float, default=18.0)
    common.add_argument("--seed", type=int, default=None)
    common.add_argument("--hid-retry", type=float, default=120.0,
                        help="seconds to keep retrying transient HID write errors")
    common.add_argument("--allow-create", action="store_true", help=argparse.SUPPRESS)
    common.add_argument("--no-leds", action="store_true", help="do not read LED reports")
    s = sub.add_parser("serve", parents=[common])
    s.add_argument("--watchdog", type=float, default=15.0)
    s.add_argument("--log", default=None)
    s.add_argument("--lock", default=None)
    t = sub.add_parser("selftest", parents=[common])
    t.add_argument("--check-device", action="store_true")
    d = sub.add_parser("demo", parents=[common])
    d.add_argument("--text", default="hello from DevRig-HID\n")
    d.add_argument("--delay", type=float, default=5.0)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.mode is None:
        args = build_parser().parse_args(["serve"] + list(argv if argv is not None else sys.argv[1:]))
    if args.mode == "selftest":
        return selftest(args)
    if args.mode == "demo":
        return demo(args)
    return Server(args).run()


if __name__ == "__main__":
    sys.exit(main())
