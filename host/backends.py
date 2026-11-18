#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/backends.py

Input backends.  Every backend ends up driving the same typing engine from
android/hid_typer.py (pacing, Poisson pauses, combos, abort, progress); they
differ only in where the 8-byte HID reports go:

  android   reports are written to /dev/hidg0 on the rooted board (real USB keyboard)
  software  reports are translated into Windows scancode key events (SendInput)
            by this PC itself - no hardware needed

LocalSession exposes exactly the interface of typer_session.TyperSession, so the
orchestrator does not care which backend is active.  Future hardware (e.g. a
Raspberry Pi Pico) = one more session class with the same four members:
``start()``, ``request()``, ``close()``, ``alive``.
"""
import argparse
import os
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "android"))
import hid_typer  # noqa: E402

from typer_session import SessionDead  # noqa: E402

# --------------------------------------------------------------------------
# HID usage -> Windows set-1 scancode  (scan, extended)
# --------------------------------------------------------------------------
_LETTER_SC = {
    "a": 0x1E, "b": 0x30, "c": 0x2E, "d": 0x20, "e": 0x12, "f": 0x21, "g": 0x22,
    "h": 0x23, "i": 0x17, "j": 0x24, "k": 0x25, "l": 0x26, "m": 0x32, "n": 0x31,
    "o": 0x18, "p": 0x19, "q": 0x10, "r": 0x13, "s": 0x1F, "t": 0x14, "u": 0x16,
    "v": 0x2F, "w": 0x11, "x": 0x2D, "y": 0x15, "z": 0x2C,
}


def build_scan_table():
    t = {}
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        t[0x04 + i] = (_LETTER_SC[c], False)
    for i in range(10):                              # 1..9, 0
        t[0x1E + i] = (0x02 + i, False)
    plain = {0x28: 0x1C, 0x29: 0x01, 0x2A: 0x0E, 0x2B: 0x0F, 0x2C: 0x39, 0x2D: 0x0C,
             0x2E: 0x0D, 0x2F: 0x1A, 0x30: 0x1B, 0x31: 0x2B, 0x33: 0x27, 0x34: 0x28,
             0x35: 0x29, 0x36: 0x33, 0x37: 0x34, 0x38: 0x35, 0x39: 0x3A}
    for u, sc in plain.items():
        t[u] = (sc, False)
    for n in range(10):                              # F1..F10
        t[0x3A + n] = (0x3B + n, False)
    t[0x44] = (0x57, False)                          # F11
    t[0x45] = (0x58, False)                          # F12
    ext = {0x49: 0x52, 0x4A: 0x47, 0x4B: 0x49, 0x4C: 0x53, 0x4D: 0x4F, 0x4E: 0x51,
           0x4F: 0x4D, 0x50: 0x4B, 0x51: 0x50, 0x52: 0x48, 0x65: 0x5D}
    for u, sc in ext.items():
        t[u] = (sc, True)
    return t


SCAN_TABLE = build_scan_table()
MOD_SCAN = {0x01: (0x1D, False), 0x02: (0x2A, False), 0x04: (0x38, False),
            0x08: (0x5B, True), 0x10: (0x1D, True), 0x20: (0x36, False),
            0x40: (0x38, True), 0x80: (0x5C, True)}


def report_events(prev, new):
    """Diff two 8-byte reports -> [(scan, extended, is_down)] in a safe order:
    key-ups, modifier-ups, modifier-downs, key-downs."""
    pm, pk = hid_typer.decode_report(prev)
    nm, nk = hid_typer.decode_report(new)
    ev = []
    for u in pk:
        if u not in nk and u in SCAN_TABLE:
            ev.append(SCAN_TABLE[u] + (False,))
    for bit, (sc, ex) in sorted(MOD_SCAN.items()):
        if (pm & bit) and not (nm & bit):
            ev.append((sc, ex, False))
    for bit, (sc, ex) in sorted(MOD_SCAN.items()):
        if (nm & bit) and not (pm & bit):
            ev.append((sc, ex, True))
    for u in nk:
        if u not in pk and u in SCAN_TABLE:
            ev.append(SCAN_TABLE[u] + (True,))
    return ev


# --------------------------------------------------------------------------
# Windows SendInput injector (hardware-scancode events, layout-independent)
# --------------------------------------------------------------------------
class WindowsSendInputDevice(object):
    """Drop-in for hid_typer.HidDevice: write() takes an 8-byte HID report."""
    leds = False
    led = None

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("the software backend injects keys with the Windows SendInput API "
                               "and therefore runs on Windows only")
        import ctypes
        from ctypes import wintypes as wt
        self._ct = ctypes
        ulong_ptr = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ulong_ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                        ("time", wt.DWORD), ("dwExtraInfo", ulong_ptr)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wt.DWORD), ("u", _U)]

        self._INPUT, self._KEYBDINPUT = INPUT, KEYBDINPUT
        self._send = ctypes.windll.user32.SendInput
        self._send.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self._send.restype = wt.UINT
        self.prev = hid_typer.ZERO_REPORT

    def _inject(self, scan, ext, down):
        flags = 0x0008                                # KEYEVENTF_SCANCODE
        if ext:
            flags |= 0x0001                           # KEYEVENTF_EXTENDEDKEY
        if not down:
            flags |= 0x0002                           # KEYEVENTF_KEYUP
        inp = self._INPUT(type=1, ki=self._KEYBDINPUT(0, scan, flags, 0, 0))
        if self._send(1, self._ct.byref(inp), self._ct.sizeof(inp)) != 1:
            raise hid_typer.HidWriteError("SendInput was rejected (target window elevated? "
                                          "run this console as the same user level)")

    def write(self, report, budget=None, ignore_abort=False):
        for scan, ext, down in report_events(self.prev, report):
            self._inject(scan, ext, down)
        self.prev = bytes(report)

    def drain_leds(self):
        return None

    def close(self):
        try:
            self.write(hid_typer.ZERO_REPORT)
        except Exception:
            pass


# --------------------------------------------------------------------------
# In-process session (same interface as typer_session.TyperSession)
# --------------------------------------------------------------------------
class LocalSession(object):
    def __init__(self, pacing, device, logger=None):
        self.pacing = pacing
        self.device = device
        self.log = logger
        self.events = queue.Queue()
        self.alive = threading.Event()
        self.last_rx = time.monotonic()
        self.stderr_tail = []
        self.server = None
        self._next_id = 1
        self._worker = None

    def start(self, ready_timeout=0):
        a = argparse.Namespace(
            device="software", wpm=self.pacing["wpm"], pause_min=self.pacing["pause_min"],
            pause_max=self.pacing["pause_max"], pause_mean_interval=self.pacing["pause_mean_interval"],
            hold_ms=self.pacing["hold_ms"], hold_sd_ms=18.0, seed=None, hid_retry=5.0,
            allow_create=False, no_leds=True, watchdog=1e9, log=None, lock=None)
        srv = hid_typer.Server(a)
        srv.dev = self.device
        srv.typist.dev = self.device
        srv.emit = self._emit
        srv.typist.emit = self._emit
        self.server = srv
        self.alive.set()
        return {"event": "ready", "version": hid_typer.VERSION + "-software",
                "python": sys.version.split()[0], "uid": 0}

    def _emit(self, obj):
        self.last_rx = time.monotonic()
        self.events.put(obj)

    def close(self):
        self.alive.clear()
        srv = self.server
        if srv is not None:
            srv.dead.set()
            if self._worker is not None:
                self._worker.join(timeout=3)
            srv.typist.release_all()
            try:
                self.device.close()
            except Exception:
                pass

    def request(self, msg, timeout=None, abort_check=None, on_event=None):
        if not self.alive.is_set() or self.server is None:
            raise SessionDead("software session is closed")
        cid = self._next_id
        self._next_id += 1
        msg = dict(msg, id=cid)
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        worker = threading.Thread(target=self.server.handle, args=(msg,), daemon=True)
        self._worker = worker
        worker.start()
        start = time.monotonic()
        abort_sent = False
        while True:
            if timeout is not None and time.monotonic() - start > timeout:
                self.server.abort_ids.add(cid)
                raise SessionDead("request %s timed out" % msg.get("cmd"))
            try:
                ev = self.events.get(timeout=0.5)
            except queue.Empty:
                if not worker.is_alive() and self.events.empty():
                    raise SessionDead("typing worker ended without a result")
                if abort_check and not abort_sent and abort_check():
                    abort_sent = True
                    with self.server.abort_lock:
                        self.server.abort_ids.add(cid)
                continue
            kind = ev.get("event")
            if kind in ("hb", "log"):
                continue
            if ev.get("id") not in (None, cid) and kind in ("ack", "typed", "aborted", "error", "progress"):
                continue
            if kind in ("ack", "typed", "aborted", "error"):
                return ev
            if on_event:
                on_event(ev)
            if abort_check and not abort_sent and abort_check():
                abort_sent = True
                with self.server.abort_lock:
                    self.server.abort_ids.add(cid)
