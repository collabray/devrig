#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/gui.py

Point-and-click front end.  Paste the repository link, choose a folder, press
Start.  The window runs host/orchestrator.py as a child process and shows its
progress; Stop asks it to halt cleanly (keys released, progress saved).

Only the Python standard library (Tkinter) is used.
"""
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ORCH = HERE / "orchestrator.py"
SETTINGS_FILE = Path.home() / ".devrig_hid_gui.json"

DEFAULTS = {"repo": "", "workspace": "", "backend": "software", "wpm": 70, "max_kb": 1024,
            "package": True, "reset": False, "non_ascii": "copy"}

PROGRESS_RE = re.compile(r"\[(\d+)/(\d+) files \| (\d+)/(\d+) typed bytes(?: \| ETA ([^\]]+))?\]")
ARCHIVE_RE = re.compile(r"DONE\s+archive:\s*(.+?)\s*$")
CURRENT_RE = re.compile(r"(?:typing|resuming)\s+(.+?)(?::| \()")
ESTIMATE_RE = re.compile(r"estimated typing time.*?:\s*([0-9d: ]+)\s*$")


# --------------------------------------------------------------- pure helpers --
def clean_repo(text):
    """Accept a pasted URL, or a whole 'git clone <url>' line."""
    t = text.strip()
    t = re.sub(r"^git\s+clone\s+", "", t, flags=re.I)
    return t.split()[0] if t else ""


def python_exe():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def build_command(s, mode="run", python=None):
    """mode: run | plan | check"""
    py = python or python_exe()
    base = [py, "-u", str(ORCH)]
    if mode == "check":
        return base + ["--check-only", "--backend", s["backend"]]
    cmd = base + [clean_repo(s["repo"]), str(s["workspace"]).strip(), "--backend", s["backend"],
                  "--wpm", str(s["wpm"]), "--max-file-bytes", str(int(s["max_kb"]) * 1024),
                  "--non-ascii", s.get("non_ascii", "copy")]
    if mode == "plan":
        cmd.append("--dry-run")
    else:
        if not s.get("package", True):
            cmd.append("--no-package")
    if s.get("reset"):
        cmd.append("--reset")
    extra = os.environ.get("DEVRIG_EXTRA_ARGS")          # advanced / test hook
    if extra:
        cmd += shlex.split(extra)
    return cmd


def control_dir(workspace):
    ws = Path(workspace).resolve()
    return ws.parent / (".devrig_" + ws.name)


def resume_info(workspace):
    """(files_done, files_total, phase) of an unfinished earlier run, else None."""
    try:
        with open(control_dir(workspace) / "state_manager.json", "r", encoding="utf-8") as fh:
            d = json.load(fh)
        files = d["plan"]["files"]
        done = sum(1 for f in files.values() if f["status"] == "done")
        return done, len(files), d["phase"]
    except (OSError, ValueError, KeyError):
        return None


def parse_line(line):
    """Return a dict describing what a log line means for the UI (or {})."""
    out = {}
    m = PROGRESS_RE.search(line)
    if m:
        out["progress"] = tuple(int(x) for x in m.groups()[:4])
        out["eta"] = (m.group(5) or "").strip()
    m = ARCHIVE_RE.search(line)
    if m:
        out["archive"] = m.group(1)
    m = CURRENT_RE.search(line)
    if m and "INFO" in line:
        out["current"] = m.group(1)
    m = ESTIMATE_RE.search(line)
    if m:
        out["estimate"] = m.group(1).strip()
    if " ERROR " in line or "FATAL" in line:
        out["level"] = "error"
    elif " WARNING " in line:
        out["level"] = "warn"
    return out


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            return dict(DEFAULTS, **json.load(fh))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=1)
    except OSError:
        pass


# ------------------------------------------------------------------------ GUI --
def run_gui(drive=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App(tk.Tk):
        def __init__(self):
            tk.Tk.__init__(self)
            self.title("DevRig-HID")
            self.geometry("860x700")
            self.minsize(720, 600)
            self.proc = None
            self.mode = None
            self.q = queue.Queue()
            self.archive = None
            self.last_error = False
            self.stop_deadline = None
            s = load_settings()
            self.v_repo = tk.StringVar(value=s["repo"])
            self.v_ws = tk.StringVar(value=s["workspace"])
            self.v_backend = tk.StringVar(value=s["backend"])
            self.v_wpm = tk.IntVar(value=s["wpm"])
            self.v_kb = tk.IntVar(value=s["max_kb"])
            self.v_pkg = tk.BooleanVar(value=s["package"])
            self.v_reset = tk.BooleanVar(value=s["reset"])
            self.v_status = tk.StringVar(value="Ready.")
            self.v_file = tk.StringVar(value="")
            self.v_resume = tk.StringVar(value="")
            self._build()
            self.v_ws.trace_add("write", lambda *_: self._refresh_resume())
            self._refresh_resume()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(100, self._poll)

        # ---- layout --------------------------------------------------------
        def _build(self):
            pad = {"padx": 10, "pady": 4}
            f = ttk.Frame(self, padding=8)
            f.pack(fill="both", expand=True)
            f.columnconfigure(1, weight=1)

            ttk.Label(f, text="1. GitHub repository link").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
            ttk.Entry(f, textvariable=self.v_repo).grid(row=1, column=0, columnspan=2, sticky="ew", **pad)
            ttk.Button(f, text="Paste", command=self._paste).grid(row=1, column=2, sticky="ew", **pad)

            ttk.Label(f, text="2. Folder to build the project in (use a new/empty folder)").grid(
                row=2, column=0, columnspan=3, sticky="w", **pad)
            ttk.Entry(f, textvariable=self.v_ws).grid(row=3, column=0, columnspan=2, sticky="ew", **pad)
            ttk.Button(f, text="Browse...", command=self._browse).grid(row=3, column=2, sticky="ew", **pad)
            ttk.Label(f, textvariable=self.v_resume, foreground="#1a6").grid(
                row=4, column=0, columnspan=3, sticky="w", padx=10)

            box = ttk.LabelFrame(f, text="3. Options", padding=8)
            box.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
            box.columnconfigure(3, weight=1)
            ttk.Label(box, text="Keyboard:").grid(row=0, column=0, sticky="w")
            ttk.Radiobutton(box, text="Software (this PC types, no hardware needed)", variable=self.v_backend,
                            value="software").grid(row=0, column=1, columnspan=3, sticky="w", padx=6)
            ttk.Radiobutton(box, text="Android board (physical USB keyboard)", variable=self.v_backend,
                            value="android").grid(row=1, column=1, columnspan=3, sticky="w", padx=6)
            ttk.Label(box, text="Typing speed (words/min):").grid(row=2, column=0, sticky="w", pady=4)
            ttk.Spinbox(box, from_=20, to=1500, increment=10, width=7, textvariable=self.v_wpm).grid(
                row=2, column=1, sticky="w", padx=6)
            ttk.Label(box, text="70 = human pace, higher = faster").grid(row=2, column=2, sticky="w")
            ttk.Label(box, text="Copy (don't type) files bigger than (KB):").grid(row=3, column=0, sticky="w")
            ttk.Spinbox(box, from_=1, to=10240, increment=10, width=7, textvariable=self.v_kb).grid(
                row=3, column=1, sticky="w", padx=6)
            ttk.Checkbutton(box, text="Create a ZIP when finished", variable=self.v_pkg).grid(
                row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
            ttk.Checkbutton(box, text="Start fresh (ignore earlier progress in this folder)",
                            variable=self.v_reset).grid(row=5, column=0, columnspan=4, sticky="w")

            bar = ttk.Frame(f)
            bar.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
            self.b_start = ttk.Button(bar, text="Start", command=self._on_start)
            self.b_start.pack(side="left", padx=(0, 6))
            self.b_stop = ttk.Button(bar, text="Stop", command=self._on_stop, state="disabled")
            self.b_stop.pack(side="left", padx=6)
            self.b_plan = ttk.Button(bar, text="Preview plan (types nothing)", command=lambda: self._begin("plan"))
            self.b_plan.pack(side="left", padx=6)
            self.b_check = ttk.Button(bar, text="Check setup", command=lambda: self._begin("check"))
            self.b_check.pack(side="left", padx=6)
            self.b_open = ttk.Button(bar, text="Open result folder", command=self._open_result, state="disabled")
            self.b_open.pack(side="right")

            warn = ("While it types: do not use the mouse or keyboard.  Emergency stop: press F12 (or Pause), "
                    "or click Stop.  Close other VS Code windows first.")
            ttk.Label(f, text=warn, foreground="#b52", wraplength=800).grid(
                row=7, column=0, columnspan=3, sticky="w", **pad)

            self.pb = ttk.Progressbar(f, mode="determinate", maximum=1000)
            self.pb.grid(row=8, column=0, columnspan=3, sticky="ew", **pad)
            ttk.Label(f, textvariable=self.v_status).grid(row=9, column=0, columnspan=3, sticky="w", padx=10)
            ttk.Label(f, textvariable=self.v_file, foreground="#555").grid(row=10, column=0, columnspan=3,
                                                                           sticky="w", padx=10)

            lf = ttk.Frame(f)
            lf.grid(row=11, column=0, columnspan=3, sticky="nsew", **pad)
            f.rowconfigure(11, weight=1)
            self.log = tk.Text(lf, height=12, wrap="word", state="disabled", font=("Consolas", 9))
            sb = ttk.Scrollbar(lf, command=self.log.yview)
            self.log.configure(yscrollcommand=sb.set)
            self.log.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.log.tag_configure("warn", foreground="#a60")
            self.log.tag_configure("error", foreground="#c00")

        # ---- helpers ---------------------------------------------------------
        def settings(self):
            def num(v, d):
                try:
                    return int(v.get())
                except (tk.TclError, ValueError):
                    return d
            return {"repo": clean_repo(self.v_repo.get()), "workspace": self.v_ws.get().strip(),
                    "backend": self.v_backend.get(), "wpm": max(5, num(self.v_wpm, 70)),
                    "max_kb": max(1, num(self.v_kb, 1024)), "package": bool(self.v_pkg.get()),
                    "reset": bool(self.v_reset.get()), "non_ascii": "copy"}

        def _paste(self):
            try:
                self.v_repo.set(clean_repo(self.clipboard_get()))
            except tk.TclError:
                pass

        def _browse(self):
            d = filedialog.askdirectory(title="Choose or create the output folder")
            if d:
                self.v_ws.set(os.path.normpath(d))

        def _refresh_resume(self):
            ws = self.v_ws.get().strip()
            info = resume_info(ws) if ws else None
            if info and info[2] != "done":
                self.v_resume.set("Earlier progress found: %d of %d files done - Start will RESUME it." % info[:2])
            elif info:
                self.v_resume.set("This folder was already completed (tick 'Start fresh' to redo it).")
            else:
                self.v_resume.set("")

        def _log(self, text, tag=None):
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n", tag or ())
            self.log.see("end")
            self.log.configure(state="disabled")

        def _set_running(self, running):
            st = "disabled" if running else "normal"
            for b in (self.b_start, self.b_plan, self.b_check):
                b.configure(state=st)
            self.b_stop.configure(state="normal" if running else "disabled")

        # ---- actions -----------------------------------------------------------
        def _validate(self, mode):
            s = self.settings()
            if mode == "check":
                return s
            if not s["repo"]:
                messagebox.showwarning("DevRig-HID", "Paste the GitHub repository link first.")
                return None
            if not s["workspace"]:
                messagebox.showwarning("DevRig-HID", "Choose the folder to build the project in.")
                return None
            ws = Path(s["workspace"])
            if mode == "run" and not s["reset"] and ws.exists() and any(ws.iterdir()) \
                    and resume_info(s["workspace"]) is None:
                messagebox.showwarning(
                    "DevRig-HID", "That folder already contains files from something else.\n"
                                  "Choose a new or empty folder.")
                return None
            return s

        def _on_start(self):
            s = self._validate("run")
            if not s:
                return
            if not messagebox.askokcancel(
                    "Ready to start",
                    "After you press OK the window minimises, VS Code opens and typing begins in 5 seconds.\n\n"
                    "- Do NOT touch the mouse or keyboard while it works.\n"
                    "- Close other VS Code windows now.\n"
                    "- Emergency stop: press F12 (or Pause).\n\nContinue?"):
                return
            save_settings(s)
            self._set_running(True)
            self.mode = "countdown"
            self.iconify()
            self._countdown(5, s)

        def _countdown(self, n, s):
            if self.mode != "countdown":
                return
            if n <= 0:
                self._launch(s, "run")
                return
            self.v_status.set("Starting in %d ..." % n)
            self.after(1000, lambda: self._countdown(n - 1, s))

        def _begin(self, mode):
            s = self._validate(mode)
            if not s:
                return
            save_settings(s)
            self._set_running(True)
            self._launch(s, mode)

        def _launch(self, s, mode):
            self.mode = mode
            self.archive = None
            self.last_error = False
            self.pb.configure(value=0)
            self.v_file.set("")
            self._log("=" * 60)
            cmd = build_command(s, mode)
            self._log("> " + " ".join('"%s"' % c if " " in c else c for c in cmd))
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                self.proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                             errors="replace", bufsize=1, creationflags=flags)
            except OSError as exc:
                self._finish(-1, "Could not start the program: %s" % exc)
                return
            self.v_status.set({"run": "Running ...", "plan": "Reading the repository ...",
                               "check": "Checking the setup ..."}[mode])
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

        def _reader(self, proc):
            for line in proc.stdout:
                self.q.put(line.rstrip("\r\n"))
            self.q.put(("exit", proc.wait()))

        def _on_stop(self):
            if self.mode == "countdown":
                self.mode = None
                self.deiconify()
                self._set_running(False)
                self.v_status.set("Cancelled.")
                return
            if not self.proc or self.proc.poll() is not None:
                return
            if self.mode in ("plan", "check"):             # nothing is typing: just end it
                self.proc.kill()
                return
            ws = self.v_ws.get().strip()
            try:
                cd = control_dir(ws)
                cd.mkdir(parents=True, exist_ok=True)
                (cd / "STOP").write_text("stop")
            except OSError:
                pass
            self.v_status.set("Stopping - releasing keys and saving progress ...")
            self.b_stop.configure(state="disabled")
            self.stop_deadline = time.monotonic() + 20

        def _poll(self):
            try:
                while True:
                    item = self.q.get_nowait()
                    if isinstance(item, tuple):
                        self._finish(item[1])
                        continue
                    self._handle_line(item)
            except queue.Empty:
                pass
            if self.stop_deadline and self.proc and self.proc.poll() is None \
                    and time.monotonic() > self.stop_deadline:
                self.proc.kill()                       # last resort; ledger is crash-safe
                self.stop_deadline = None
            self.after(150, self._poll)

        def _handle_line(self, line):
            info = parse_line(line)
            self._log(line, info.get("level"))
            if info.get("level") == "error":
                self.last_error = True
            if "progress" in info:
                fd, ft, bd, bt = info["progress"]
                frac = (bd / bt) if bt else (fd / ft if ft else 0)
                self.pb.configure(value=min(1000, int(frac * 1000)))
                eta = ("   ETA " + info["eta"]) if info.get("eta") else ""
                self.v_status.set("Files %d/%d   |   typed %d of %d bytes (%d%%)%s"
                                  % (fd, ft, bd, bt, int(frac * 100), eta))
            if "current" in info:
                self.v_file.set("Now: " + info["current"])
            if "estimate" in info and self.mode == "plan":
                self.v_status.set("Estimated typing time: " + info["estimate"])
            if "archive" in info:
                self.archive = info["archive"]

        def _finish(self, code, message=None):
            mode, self.mode = self.mode, None
            self.proc = None
            self.stop_deadline = None
            self._set_running(False)
            self.deiconify()
            self.lift()
            self._refresh_resume()
            if message:
                self.v_status.set(message)
                messagebox.showerror("DevRig-HID", message)
            elif mode == "plan":
                self.v_status.set("Plan ready (see the log). Nothing was typed.")
            elif mode == "check":
                ok = code == 0
                self.v_status.set("Setup OK." if ok else "Setup problem - read the red lines in the log.")
                (messagebox.showinfo if ok else messagebox.showwarning)(
                    "DevRig-HID", "Setup looks good." if ok else "The setup check found a problem.\n"
                                                                  "See the red lines in the log.")
            elif code == 0:
                self.pb.configure(value=1000)
                self.v_status.set("Finished.")
                if self.archive:
                    self.b_open.configure(state="normal")
                messagebox.showinfo("DevRig-HID", "Finished!" + (("\n\nZIP created:\n" + self.archive)
                                                                 if self.archive else ""))
            elif code == 130:
                self.v_status.set("Stopped. Progress is saved - press Start to resume.")
                messagebox.showinfo("DevRig-HID", "Stopped. Progress is saved.\nPress Start again to resume.")
            else:
                self.v_status.set("Stopped with an error - see the red lines in the log.")
                messagebox.showerror("DevRig-HID", "It stopped because of a problem.\n"
                                                   "Progress is saved. Read the red lines in the log, fix the "
                                                   "problem and press Start to resume.")

        def _open_result(self):
            target = self.archive and os.path.dirname(self.archive) or self.v_ws.get().strip()
            if target and os.path.isdir(target):
                if os.name == "nt":
                    os.startfile(target)                      # noqa: S606
                else:
                    subprocess.Popen(["xdg-open", target])

        def _on_close(self):
            if self.proc and self.proc.poll() is None:
                if not messagebox.askyesno("DevRig-HID", "It is still running. Stop it and close the window?"):
                    return
                self._on_stop()
                end = time.monotonic() + 12
                while self.proc and self.proc.poll() is None and time.monotonic() < end:
                    time.sleep(0.2)
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
            self.destroy()

    app = App()
    if drive:
        return drive(app)
    app.mainloop()


if __name__ == "__main__":
    run_gui()
