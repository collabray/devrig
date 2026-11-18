#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/state_manager.py

Crash-safe state ledger (``state_manager.json``).

* Every write is atomic (temp file + fsync + os.replace) and the previous
  generation is kept as ``state_manager.json.bak``.
* The ledger records the run identity, the execution phase, the complete file
  plan, per-file status / byte offsets, created directories and the package.
* A cross-process lock prevents two orchestrators from driving the same rig.
"""
import json
import os
import shutil
import threading
import time
from datetime import datetime

SCHEMA = 1

PHASES = ("init", "cloned", "planned", "typing", "audit", "packaging", "done")
DONE_STATUSES = ("done",)


class StateCorrupt(RuntimeError):
    pass


class StateMismatch(RuntimeError):
    pass


class LockError(RuntimeError):
    pass


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_text(path, text):
    path = str(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)
    if os.name != "nt":
        try:
            dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


class ProcessLock(object):
    """Exclusive, advisory, crash-safe lock (released by the OS on exit)."""

    def __init__(self, path):
        self.path = str(path)
        self._fh = None

    def acquire(self):
        fh = open(self.path, "a+")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            fh.seek(0)
            owner = fh.read().strip()
            fh.close()
            raise LockError("another DevRig-HID orchestrator is already running for this "
                            "workspace (lock %s, owner pid %s)" % (self.path, owner or "?"))
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
        except (IOError, OSError):
            pass
        fh.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class StateManager(object):
    def __init__(self, path, logger=None):
        self.path = str(path)
        self.log = logger
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._dirty = False
        self.data = None

    # ------------------------------------------------------------------ io --
    def exists(self):
        return os.path.exists(self.path) or os.path.exists(self.path + ".bak")

    def load(self):
        """Load the ledger; fall back to the .bak generation if needed."""
        errors = []
        for candidate in (self.path, self.path + ".bak"):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("schema") != SCHEMA:
                    raise StateCorrupt("unsupported schema %r" % data.get("schema"))
                self.data = data
                if candidate != self.path and self.log:
                    self.log.warning("state ledger restored from backup generation %s", candidate)
                return True
            except (ValueError, OSError, StateCorrupt) as exc:
                errors.append("%s: %s" % (candidate, exc))
        if errors:
            raise StateCorrupt("state ledger unreadable (%s). Re-run with --reset to start over."
                               % "; ".join(errors))
        return False

    def init_new(self, repo_url, workspace, branch):
        with self._lock:
            self.data = {
                "schema": SCHEMA,
                "created_at": _now(),
                "updated_at": _now(),
                "run_count": 1,
                "phase": "init",
                "identity": {"repo_url": repo_url, "workspace": str(workspace),
                             "branch": branch, "commit": None},
                "plan": {"order": [], "files": {}},
                "directories": [],
                "completed_files": [],
                "current_file": None,
                "rig": {},
                "package": None,
                "history": [],
            }
            self.save(force=True)

    def check_identity(self, repo_url, workspace):
        ident = self.data["identity"]
        same_ws = os.path.normcase(os.path.abspath(ident["workspace"])) == \
            os.path.normcase(os.path.abspath(str(workspace)))
        if ident["repo_url"] != repo_url or not same_ws:
            raise StateMismatch(
                "the existing ledger belongs to a different run (repo %s, workspace %s). "
                "Use --reset to discard it." % (ident["repo_url"], ident["workspace"]))

    def save(self, force=False):
        with self._lock:
            self.data["updated_at"] = _now()
            atomic_write_text(self.path, json.dumps(self.data, indent=1, sort_keys=False))
            self._last_save = time.monotonic()
            self._dirty = False

    def flush(self):
        with self._lock:
            if self._dirty:
                self.save()

    def _maybe_save(self, interval=1.0):
        self._dirty = True
        if time.monotonic() - self._last_save >= interval:
            self.save()

    # ----------------------------------------------------------- run level --
    @property
    def phase(self):
        return self.data["phase"]

    def set_phase(self, phase):
        assert phase in PHASES
        with self._lock:
            if self.data["phase"] != phase:
                self.event("phase %s -> %s" % (self.data["phase"], phase))
            self.data["phase"] = phase
            self.save()

    def bump_run(self):
        with self._lock:
            self.data["run_count"] = self.data.get("run_count", 0) + 1
            self.save()

    def event(self, text):
        with self._lock:
            hist = self.data.setdefault("history", [])
            hist.append({"t": _now(), "msg": text})
            del hist[:-200]

    def set_commit(self, commit):
        with self._lock:
            self.data["identity"]["commit"] = commit
            self.save()

    def set_rig(self, **info):
        with self._lock:
            self.data["rig"].update(info)
            self.save()

    def set_package(self, info):
        with self._lock:
            self.data["package"] = info
            self.save()

    # ----------------------------------------------------------------- plan --
    def set_plan(self, entries):
        with self._lock:
            self.data["plan"] = {
                "order": [e["path"] for e in entries],
                "files": dict((e["path"], e) for e in entries),
            }
            self.save()

    def has_plan(self):
        return bool(self.data and self.data["plan"]["order"])

    def entry(self, path):
        return self.data["plan"]["files"][path]

    def entries(self):
        files = self.data["plan"]["files"]
        return [files[p] for p in self.data["plan"]["order"]]

    def pending_paths(self):
        files = self.data["plan"]["files"]
        return [p for p in self.data["plan"]["order"] if files[p]["status"] not in DONE_STATUSES]

    def update_entry(self, path, **fields):
        with self._lock:
            ent = self.data["plan"]["files"][path]
            ent.update(fields)
            if fields.get("status") == "done":
                if path not in self.data["completed_files"]:
                    self.data["completed_files"].append(path)
                if self.data["current_file"] == path:
                    self.data["current_file"] = None
            elif "status" in fields and path in self.data["completed_files"]:
                self.data["completed_files"].remove(path)
            self.save()

    def begin_file(self, path):
        with self._lock:
            self.data["current_file"] = path
            ent = self.data["plan"]["files"][path]
            if ent["status"] == "pending":
                ent["status"] = "typing"
            self.save()

    def note_progress(self, path, offset):
        with self._lock:
            self.data["plan"]["files"][path]["offset"] = int(offset)
            self._maybe_save(1.0)

    def add_directory(self, rel):
        with self._lock:
            if rel not in self.data["directories"]:
                self.data["directories"].append(rel)
                self._maybe_save(1.0)

    def stats(self):
        ents = self.entries()
        total = len(ents)
        done = sum(1 for e in ents if e["status"] in DONE_STATUSES)
        typed_total = sum(e["size"] for e in ents if e["mode"] == "type")
        typed_done = sum(e["size"] for e in ents if e["mode"] == "type" and e["status"] in DONE_STATUSES)
        cur = self.data.get("current_file")
        if cur and cur in self.data["plan"]["files"]:
            ce = self.data["plan"]["files"][cur]
            if ce["mode"] == "type" and ce["status"] not in DONE_STATUSES:
                typed_done += min(ce.get("offset", 0), ce["size"])
        return {"files_total": total, "files_done": done,
                "typed_bytes_total": typed_total, "typed_bytes_done": typed_done}
