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
WS_HINT = None                      # folder name of OUR workspace (distinguishes it from other VS Code windows)


def set_workspace_hint(name):
    global WS_HINT
    WS_HINT = name

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
    try:
        _user32.SetProcessDPIAware()                   # real pixel coordinates for GetWindowRect/SetCursorPos
    except Exception:
        pass


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
    if WS_HINT and WS_HINT not in title:
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
            if (t.endswith(VSCODE_MARK) or (VSCODE_MARK + " -") in t) and (not WS_HINT or WS_HINT in t):
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


def panic_pressed():
    """Emergency stop: physical F12 or Pause/Break (the rig never injects either)."""
    if not AVAILABLE:
        return False
    return bool(_user32.GetAsyncKeyState(0x7B) & 0x8000) or bool(_user32.GetAsyncKeyState(0x13) & 0x8000)


def click_editor():
    """Maximise our VS Code window and click its editor area so the text area owns the keyboard."""
    if not AVAILABLE:
        return False
    wins = find_vscode_windows()
    if not wins:
        return False
    hwnd = wins[0]
    _user32.ShowWindow(hwnd, 3)                           # SW_MAXIMIZE
    bring_to_front(hwnd)
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    x = rect.left + int((rect.right - rect.left) * 0.58)
    y = rect.top + int((rect.bottom - rect.top) * 0.55)
    _user32.SetCursorPos(x, y)
    _user32.mouse_event(0x0002, 0, 0, 0, 0)               # left down
    _user32.mouse_event(0x0004, 0, 0, 0, 0)               # left up
    return True
