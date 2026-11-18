#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/packager.py

Post-build packaging.  Verifies that no process still holds the workspace
files open, compresses the finished tree into a timestamped ZIP (written to a
temporary ``.part`` file, fully re-verified, then atomically renamed) and
emits a SHA-256 sidecar plus a JSON manifest.

Usage:  python packager.py <workspace> [--archive-dir DIR] [--name NAME]
"""
import argparse
import fnmatch
import hashlib
import json
import os
import stat
import sys
import time
import zipfile
from datetime import datetime


class PackagingError(RuntimeError):
    pass


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def collect(workspace, exclude=(), skip_paths=()):
    """Return (files, dirs) as sorted lists of relative POSIX paths."""
    workspace = os.path.abspath(workspace)
    skip = set(os.path.normcase(os.path.abspath(p)) for p in skip_paths)
    files, dirs = [], []
    for root, dnames, fnames in os.walk(workspace, followlinks=False):
        dnames.sort()
        rel_root = os.path.relpath(root, workspace)
        rel_root = "" if rel_root == "." else rel_root.replace(os.sep, "/")
        keep = []
        for d in dnames:
            rel = (rel_root + "/" + d) if rel_root else d
            full = os.path.join(root, d)
            if os.path.islink(full) or any(fnmatch.fnmatch(rel, p) for p in exclude):
                continue
            keep.append(d)
            dirs.append(rel)
        dnames[:] = keep
        for f in sorted(fnames):
            rel = (rel_root + "/" + f) if rel_root else f
            full = os.path.join(root, f)
            if os.path.normcase(os.path.abspath(full)) in skip:
                continue
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(f, p) for p in exclude):
                continue
            files.append(rel)
    # directories that contain nothing archived are kept as explicit entries
    non_empty = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            non_empty.add("/".join(parts[:i]))
    empty_dirs = [d for d in dirs if d not in non_empty]
    return sorted(files), sorted(empty_dirs)


def wait_for_unlocked(workspace, files, timeout=60.0, log=None):
    """Release check: every file must be openable for writing (no editor lock)."""
    end = time.monotonic() + timeout
    pending = list(files)
    while pending:
        still = []
        for rel in pending:
            p = os.path.join(workspace, rel.replace("/", os.sep))
            try:
                if os.access(p, os.W_OK):
                    with open(p, "ab"):
                        pass
                else:
                    with open(p, "rb"):
                        pass
            except (PermissionError, OSError):
                still.append(rel)
        pending = still
        if not pending:
            return
        if time.monotonic() > end:
            raise PackagingError("files still locked by another process: %s"
                                 % ", ".join(pending[:5]))
        if log:
            log.info("waiting for %d file handle(s) to be released ...", len(pending))
        time.sleep(1.0)


def create_archive(workspace, archive_dir=None, name=None, exclude=(), level=6,
                   include_root=True, log=None, lock_timeout=60.0):
    workspace = os.path.abspath(workspace)
    if not os.path.isdir(workspace):
        raise PackagingError("workspace does not exist: %s" % workspace)
    archive_dir = os.path.abspath(archive_dir or os.path.dirname(workspace))
    os.makedirs(archive_dir, exist_ok=True)
    base = name or os.path.basename(workspace.rstrip("\\/")) or "workspace"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = os.path.join(archive_dir, "%s_%s.zip" % (base, stamp))
    n = 1
    while os.path.exists(final):
        final = os.path.join(archive_dir, "%s_%s-%d.zip" % (base, stamp, n))
        n += 1
    part = final + ".part"

    files, empty_dirs = collect(workspace, exclude, skip_paths=(part, final))
    if not files and not empty_dirs:
        raise PackagingError("workspace is empty; nothing to package")
    wait_for_unlocked(workspace, files, timeout=lock_timeout, log=log)

    root = (base + "/") if include_root else ""
    manifest_files = {}
    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=level, allowZip64=True) as zf:
            for d in empty_dirs:
                zi = zipfile.ZipInfo(root + d + "/", date_time=time.localtime()[:6])
                zi.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                zf.writestr(zi, b"")
            for rel in files:
                full = os.path.join(workspace, rel.replace("/", os.sep))
                zi = zipfile.ZipInfo.from_file(full, root + rel, strict_timestamps=False)
                zi.compress_type = zipfile.ZIP_DEFLATED
                h = hashlib.sha256()
                with open(full, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        h.update(block)
                        dst.write(block)
                manifest_files[rel] = {"sha256": h.hexdigest(), "size": os.path.getsize(full)}
        verify_archive(part, root, manifest_files, empty_dirs)
        os.replace(part, final)
    except BaseException:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        raise

    digest = sha256_file(final)
    with open(final + ".sha256", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("%s *%s\n" % (digest, os.path.basename(final)))
    info = {
        "archive": final,
        "sha256": digest,
        "size": os.path.getsize(final),
        "files": len(files),
        "empty_directories": len(empty_dirs),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root_folder": base if include_root else None,
    }
    with open(final + ".manifest.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(dict(info, manifest=manifest_files), fh, indent=1)
    if log:
        log.info("archive ready: %s (%d files, %d bytes, sha256 %s)",
                 final, len(files), info["size"], digest[:16])
    return info


def verify_archive(zip_path, root, manifest_files, empty_dirs=()):
    """Full re-read: CRC test, exact member set, per-file SHA-256."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise PackagingError("archive CRC failure in member %s" % bad)
        names = set(n for n in zf.namelist() if not n.endswith("/"))
        expected = set(root + rel for rel in manifest_files)
        if names != expected:
            diff = sorted(names ^ expected)[:5]
            raise PackagingError("archive member set differs from workspace: %s" % diff)
        for rel, meta in manifest_files.items():
            h = hashlib.sha256()
            with zf.open(root + rel) as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            if h.hexdigest() != meta["sha256"]:
                raise PackagingError("archive content hash mismatch for %s" % rel)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Package a DevRig-HID workspace into a timestamped ZIP")
    ap.add_argument("workspace")
    ap.add_argument("--archive-dir")
    ap.add_argument("--name")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--level", type=int, default=6)
    ap.add_argument("--no-root-folder", action="store_true")
    a = ap.parse_args(argv)
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        info = create_archive(a.workspace, a.archive_dir, a.name, a.exclude, a.level,
                              not a.no_root_folder, log=logging.getLogger("packager"))
    except PackagingError as exc:
        print("packaging failed: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
