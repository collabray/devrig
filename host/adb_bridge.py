#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/adb_bridge.py

Thin, defensive wrapper around the ``adb`` command line.  Classifies failures
into "device went away" (recoverable) and "everything else".
"""
import os
import re
import shlex
import subprocess
import time
from collections import namedtuple

Result = namedtuple("Result", "rc out err")

_ADB_LEVEL_ERROR = re.compile(r"^(adb: |error: )", re.M)
_DISCONNECT_HINTS = (
    "no devices/emulators found", "device offline", "not found", "closed",
    "protocol fault", "device still authorizing", "cannot connect", "connection reset",
    "broken pipe", "transport", "unable to connect", "device is offline",
    "daemon not running", "failed to read copy response", "no such device",
)


class AdbError(RuntimeError):
    pass


class AdbDisconnected(AdbError):
    """The device vanished / went offline (recoverable)."""


class AdbUnauthorized(AdbError):
    pass


def _flags():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


class Adb(object):
    def __init__(self, adb_path="adb", serial=None, logger=None):
        self.adb_path = adb_path
        self.serial = serial
        self.log = logger
        self.su_style = None

    # ----------------------------------------------------------- low level --
    def _argv(self, args, with_serial=True):
        argv = [self.adb_path]
        if with_serial and self.serial:
            argv += ["-s", self.serial]
        return argv + list(args)

    def run(self, *args, **kw):
        timeout = kw.pop("timeout", 60)
        check = kw.pop("check", True)
        with_serial = kw.pop("with_serial", True)
        argv = self._argv(args, with_serial)
        try:
            cp = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, **_flags())
        except subprocess.TimeoutExpired:
            raise AdbDisconnected("adb %s timed out after %ss" % (" ".join(args[:2]), timeout))
        except OSError as exc:
            raise AdbError("cannot execute %s: %s" % (self.adb_path, exc))
        out = cp.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        err = cp.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
        res = Result(cp.returncode, out, err)
        if check and cp.returncode != 0:
            self._raise(args, res)
        return res

    def _raise(self, args, res):
        text = (res.err + "\n" + res.out).strip()
        if "unauthorized" in text:
            raise AdbUnauthorized("device unauthorized: accept the RSA key prompt on the board")
        if _ADB_LEVEL_ERROR.search(res.err) and any(h in res.err.lower() for h in _DISCONNECT_HINTS):
            raise AdbDisconnected("adb %s: %s" % (" ".join(args[:3]), text))
        raise AdbError("adb %s failed (rc=%d): %s" % (" ".join(args[:3]), res.rc, text))

    # ------------------------------------------------------------- devices --
    def start_server(self):
        self.run("start-server", with_serial=False, timeout=30)

    def list_devices(self):
        res = self.run("devices", with_serial=False, timeout=20)
        devices = []
        for line in res.out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and not line.startswith("*"):
                devices.append((parts[0], parts[1]))
        return devices

    def select_device(self):
        devs = self.list_devices()
        if self.serial:
            states = dict(devs)
            if self.serial not in states:
                raise AdbDisconnected("device %s is not attached (attached: %s)"
                                      % (self.serial, ", ".join(s for s, _ in devs) or "none"))
            state = states[self.serial]
        else:
            if not devs:
                raise AdbDisconnected("no Android device attached (check USB cable / adb connect)")
            if len(devs) > 1:
                raise AdbError("several devices attached (%s); choose one with --serial"
                               % ", ".join(s for s, _ in devs))
            state = devs[0][1]
        if state == "unauthorized":
            raise AdbUnauthorized("device unauthorized: accept the RSA key prompt on the board")
        if state != "device":
            raise AdbDisconnected("device is in state '%s'" % state)
        return self.serial or devs[0][0]

    def wait_for_device(self, timeout=120, interval=1.0):
        end = time.monotonic() + timeout
        last = "no device"
        while time.monotonic() < end:
            try:
                self.select_device()
                res = self.run("get-state", timeout=10, check=False)
                if res.rc == 0 and "device" in res.out:
                    return True
            except AdbUnauthorized:
                raise
            except AdbError as exc:
                last = str(exc)
            time.sleep(interval)
        raise AdbDisconnected("device did not come back within %ss (%s)" % (timeout, last))

    # --------------------------------------------------------------- shell --
    def shell(self, command, timeout=60, check=True):
        return self.run("shell", command, timeout=timeout, check=check)

    def _su_string(self, command):
        q = shlex.quote(command)
        if self.su_style == "0":
            return "su 0 sh -c %s" % q
        return "su -c %s" % q

    def su(self, command, timeout=60, check=True):
        return self.run("shell", self._su_string(command), timeout=timeout, check=check)

    def has_root(self):
        for style in ("c", "0"):
            self.su_style = style
            try:
                res = self.su("id", timeout=30, check=False)
            except AdbDisconnected:
                raise
            except AdbError:
                continue
            if res.rc == 0 and "uid=0" in res.out:
                return True
        self.su_style = None
        return False

    def popen_su(self, command):
        argv = self._argv(["shell", self._su_string(command)])
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0, **_flags())

    # ----------------------------------------------------------- transfers --
    def push(self, local, remote, timeout=120):
        self.run("push", str(local), remote, timeout=timeout)

    def read_remote_text(self, remote, timeout=30):
        res = self.su("cat %s 2>/dev/null; true" % shlex.quote(remote), timeout=timeout, check=False)
        return res.out

    # -------------------------------------------------------------- python --
    TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
    TERMUX_LIB = "/data/data/com.termux/files/usr/lib"

    def python_candidates(self, preferred=None):
        env_termux = ("export PATH=%s:$PATH LD_LIBRARY_PATH=%s:$LD_LIBRARY_PATH; "
                      % (self.TERMUX_BIN, self.TERMUX_LIB))
        cands = []
        if preferred:
            env = env_termux if "com.termux" in preferred else ""
            cands.append((preferred, env))
        cands += [("python3", ""), ("python", ""), ("/system/bin/python3", ""),
                  ("/data/adb/modules/python/system/bin/python3", ""),
                  (self.TERMUX_BIN + "/python3", env_termux)]
        return cands

    def detect_python(self, preferred=None):
        """Return (python_path, env_prefix) of a usable Python 3 on the board."""
        for py, env in self.python_candidates(preferred):
            res = self.su("%s%s --version 2>&1" % (env, py), timeout=30, check=False)
            text = (res.out + res.err).strip()
            m = re.search(r"Python (\d+)\.(\d+)", text)
            if res.rc == 0 and m and int(m.group(1)) == 3 and int(m.group(2)) >= 6:
                return py, env
        return None
