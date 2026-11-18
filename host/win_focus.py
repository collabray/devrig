#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/win_focus.py

Windows foreground-window guard (ctypes, no third-party packages).  Keystrokes
from the HID rig go to whichever window is in the foreground, so the
orchestrator verifies - and if necessary restores - VS Code focus and checks
that the US keyboard layout is active.  On non-Windows hosts every function
is a harmless no-op and ``AVAILABLE`` is False.
"""
import os

AVAILABLE = os.name == "nt"
VSCODE_MARK = "Visual Studio Code"

if AVAILABLE:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
    _user32.GetKeyboardLayout.restype = wintypes.HKL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _title(hwnd):
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def foreground_title():
    if not AVAILABLE:
        return None
    hwnd = _user32.GetForegroundWindow()
    return _title(hwnd) if hwnd else ""


def foreground_is_vscode(file_hint=None):
    """True when the foreground window is VS Code (and shows ``file_hint``)."""
    if not AVAILABLE:
        return True
    title = foreground_title() or ""
    if VSCODE_MARK not in title:
        return False
    return True if not file_hint else (file_hint in title)


def keyboard_layout_id():
    """Language id (low word of the HKL) of the foreground thread, e.g. 0x0409."""
    if not AVAILABLE:
        return None
    hwnd = _user32.GetForegroundWindow()
    tid = _user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
    return int(_user32.GetKeyboardLayout(tid)) & 0xFFFF


def find_vscode_windows():
    if not AVAILABLE:
        return []
    found = []

    def cb(hwnd, _lp):
        if _user32.IsWindowVisible(hwnd):
            t = _title(hwnd)
            if t.endswith(VSCODE_MARK) or (VSCODE_MARK + " -") in t:
                found.append(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found


def bring_to_front(hwnd):
    """Raise a window without synthesising keyboard input (AttachThreadInput)."""
    if not AVAILABLE or not hwnd:
        return False
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, 9)                      # SW_RESTORE
    fg = _user32.GetForegroundWindow()
    fg_tid = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_tid = _kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(_user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        _user32.BringWindowToTop(hwnd)
        ok = bool(_user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            _user32.AttachThreadInput(cur_tid, fg_tid, False)
    return ok


def focus_vscode():
    wins = find_vscode_windows()
    return bring_to_front(wins[0]) if wins else False
