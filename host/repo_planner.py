#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/repo_planner.py

Clones the GitHub repository and turns its file tree into a deterministic
typing plan.

Each plan entry has a mode:
  type   text file that will be typed through the HID keyboard
  copy   file that cannot (or should not) be typed and is copied verbatim:
         binaries, oversize files, bulk/generated files, files holding
         characters that have no US-keyboard scancode (see --non-ascii)
  empty  zero-byte file
"""
import fnmatch
import hashlib
import os
import posixpath
import re
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "android"))
import hid_typer  # noqa: E402  (single source of truth for the scancode map)

DEFAULT_COPY_GLOBS = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "*.lock", "*.min.js",
    "*.min.css", "*.map",
]

_TRANSLIT = {
    "\u2018": "'", "\u2019": "'", "\u201a": ",", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "--", "\u2212": "-", "\u2026": "...", "\u00a0": " ",
    "\u2022": "*", "\u00d7": "x", "\u2192": "->", "\u2190": "<-", "\u21d2": "=>",
    "\u00a9": "(c)", "\u00ae": "(R)", "\u2122": "(TM)", "\u00b0": " deg",
    "\u2264": "<=", "\u2265": ">=", "\u2260": "!=", "\ufeff": "", "\u200b": "",
}


class PlanError(RuntimeError):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


# ------------------------------------------------------------------ git ----
def normalize_repo_url(url):
    """Return (clone_url, branch_or_None) for GitHub web URLs and git URLs."""
    url = url.strip()
    m = re.match(r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?"
                 r"(?:/tree/([^\s#?]+?))?/?(?:[?#].*)?$", url)
    if m:
        owner, repo, branch = m.group(1), m.group(2), m.group(3)
        return "https://github.com/%s/%s.git" % (owner, repo), branch
    return url, None


def _git(git, args, cwd=None, check=True, timeout=1800):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    cp = subprocess.run([git] + list(args), cwd=cwd, env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=timeout)
    out = cp.stdout.decode("utf-8", "replace")
    err = cp.stderr.decode("utf-8", "replace")
    if check and cp.returncode != 0:
        raise PlanError("git %s failed (rc=%d): %s" % (" ".join(args), cp.returncode,
                                                       (err or out).strip()))
    return cp.returncode, out, err


def ensure_clone(git, clone_url, branch, dest, pinned_commit=None, log=None):
    """Clone (or reuse) the repository at ``dest``; returns the HEAD commit."""
    dest = str(dest)
    if os.path.isdir(os.path.join(dest, ".git")):
        rc, out, _ = _git(git, ["rev-parse", "HEAD"], cwd=dest, check=False)
        head = out.strip()
        if rc == 0 and (pinned_commit is None or head == pinned_commit):
            if log:
                log.info("reusing existing clone at %s (%s)", dest, head[:10])
            return head
        if pinned_commit:
            if log:
                log.info("re-aligning clone to pinned commit %s", pinned_commit[:10])
            _git(git, ["fetch", "--depth", "1", "origin", pinned_commit], cwd=dest, check=False)
            rc, _, _ = _git(git, ["checkout", "--force", pinned_commit], cwd=dest, check=False)
            if rc == 0:
                return pinned_commit
        raise PlanError("existing clone at %s does not match the recorded commit" % dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    args = ["clone", "--depth", "1", "--no-tags"]
    if branch:
        args += ["--branch", branch]
    args += [clone_url, dest]
    if log:
        log.info("cloning %s%s", clone_url, (" (branch %s)" % branch) if branch else "")
    _git(git, args)
    if pinned_commit:
        _git(git, ["fetch", "--depth", "1", "origin", pinned_commit], cwd=dest, check=False)
        rc, _, _ = _git(git, ["checkout", "--force", pinned_commit], cwd=dest, check=False)
        if rc != 0:
            raise PlanError("recorded commit %s is no longer available upstream" % pinned_commit)
    return _git(git, ["rev-parse", "HEAD"], cwd=dest)[1].strip()


def list_tracked_files(git, repo_dir):
    _, out, _ = _git(git, ["ls-files", "-z"], cwd=repo_dir)
    return [p for p in out.split("\0") if p]


# ------------------------------------------------------------- classify ----
def _matches(rel, patterns):
    base = posixpath.basename(rel)
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p) for p in patterns)


def transliterate(text):
    out = []
    for ch in text:
        if ch in hid_typer.CHAR_MAP:
            out.append(ch)
        elif ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        else:
            dec = unicodedata.normalize("NFKD", ch)
            dec = "".join(c for c in dec if c in hid_typer.CHAR_MAP)
            if dec:
                out.append(dec)
            elif unicodedata.category(ch).startswith("C"):
                continue                        # control / format characters: drop
            else:
                out.append("?")
    return "".join(out)


def prepare_content(rel, data, cfg):
    """Return (mode, final_bytes, note) for one file."""
    if len(data) == 0:
        return "empty", b"", ""
    if _matches(rel, cfg["copy_globs"]):
        return "copy", data, "bulk/generated file (copy-glob)"
    if len(data) > cfg["max_file_bytes"]:
        return "copy", data, "larger than --max-file-bytes"
    if b"\x00" in data[:8192]:
        return "copy", data, "binary"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "copy", data, "not valid UTF-8"
    note = ""
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        note = "line endings normalised to LF"
    bad = hid_typer.untypeable(text)
    if bad:
        policy = cfg["non_ascii"]
        if policy == "copy":
            return "copy", data, "contains characters without a US-keyboard scancode"
        if policy == "error":
            raise PlanError("%s contains %d untypeable character(s), first %r at index %d"
                            % (rel, len(bad), bad[0][1], bad[0][0]))
        text = transliterate(text)
        note = (note + "; " if note else "") + "transliterated to ASCII"
    return "type", text.encode("ascii"), note


def build_plan(repo_dir, files, cfg):
    entries = []
    for rel in sorted(files, key=lambda p: (p.count("/"), p.lower())):
        src = os.path.join(repo_dir, rel.replace("/", os.sep))
        if cfg["include"] and not _matches(rel, cfg["include"]):
            continue
        if cfg["exclude"] and _matches(rel, cfg["exclude"]):
            continue
        if os.path.islink(src) or not os.path.isfile(src):
            entries.append(_entry(rel, "skip", 0, "", "symlink/submodule/not a regular file"))
            continue
        with open(src, "rb") as fh:
            data = fh.read()
        mode, final, note = prepare_content(rel, data, cfg)
        entries.append(_entry(rel, mode, len(final), sha256_bytes(final), note))
    return entries


def _entry(rel, mode, size, digest, note):
    return {"path": rel, "mode": mode, "size": size, "sha256": digest, "note": note,
            "status": "done" if mode == "skip" else "pending", "offset": 0, "attempts": 0}


def final_content(repo_dir, entry, cfg):
    """Recompute the exact bytes the workspace file must contain."""
    src = os.path.join(repo_dir, entry["path"].replace("/", os.sep))
    with open(src, "rb") as fh:
        data = fh.read()
    mode, final, _ = prepare_content(entry["path"], data, cfg)
    if mode != entry["mode"] or sha256_bytes(final) != entry["sha256"]:
        raise PlanError("source file %s changed since the plan was created" % entry["path"])
    return final


def estimate_seconds(entries, wpm):
    chars = sum(e["size"] for e in entries if e["mode"] == "type")
    return chars / (wpm * 5.0 / 60.0) * 1.08
