#!/usr/bin/env python3
"""Drives the REAL window (virtual display) through plan -> run -> stop -> resume.
Run:  xvfb-run -a python tests/test_gui.py      (skips when tkinter/DISPLAY is unavailable)"""
import os, shutil, subprocess, sys, tempfile, time, zipfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "host"), HERE]
try:
    import tkinter
    from tkinter import messagebox
except ImportError:
    print("SKIP: tkinter missing"); sys.exit(0)
if os.name != "nt" and not os.environ.get("DISPLAY"):
    print("SKIP: no display"); sys.exit(0)
import gui
from e2e_sim import make_repo

def pump(app, until, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        app.update(); time.sleep(0.05)
        if until(): return True
    raise AssertionError("timeout; status=%r" % app.v_status.get())

def main():
    for n in ("showinfo", "showwarning", "showerror", "askokcancel", "askyesno"):
        setattr(messagebox, n, lambda *a, **k: True)
    tmp = tempfile.mkdtemp(prefix="devrig_gui_"); fake = os.path.join(tmp, "fake"); os.makedirs(fake)
    repo = os.path.join(tmp, "up"); make_repo(repo)
    ws = os.path.join(tmp, "out", "demo"); os.makedirs(os.path.dirname(ws))
    open(os.path.join(fake, "hidg0"), "wb").close()
    os.environ.update(FAKEROOT=fake, PATH=os.path.join(HERE, "sim", "bin") + os.pathsep + os.environ["PATH"],
        DEVRIG_EXTRA_ARGS="--software-sink %s --timing-scale 0.02 --settle 2 --file-gap-min 0 --file-gap-max 0.05 "
                          "--pause-min 0.05 --pause-max 0.1 --pause-mean-interval 1 --hold-ms 4" % os.path.join(fake, "hidg0"))
    gui.SETTINGS_FILE = type(gui.SETTINGS_FILE)(os.path.join(tmp, "gui.json"))
    cons = subprocess.Popen([sys.executable, os.path.join(HERE, "sim", "consumer.py")], env=os.environ)

    def drive(app):
        app.v_repo.set("git clone file://" + repo); app.v_ws.set(ws); app.v_wpm.set(2500)
        done = lambda: app.proc is None and app.mode is None
        # 1. preview
        app._begin("plan"); pump(app, done)
        assert "Plan ready" in app.v_status.get(), app.v_status.get()
        log = app.log.get("1.0", "end"); assert "plan: 9 files" in log, log[-400:]
        assert not os.path.exists(os.path.join(ws, "README.md")), "preview must type nothing"
        print("GUI preview OK")
        # 2. run, then Stop part-way
        app._set_running(True); app._launch(app.settings(), "run")
        pump(app, lambda: "typing src/util/helpers.js" in app.log.get("1.0", "end"))
        app._on_stop(); pump(app, done, 60)
        assert "Stopped" in app.v_status.get(), app.v_status.get()
        assert "Earlier progress found" in app.v_resume.get(), app.v_resume.get()
        print("GUI stop OK ->", app.v_resume.get())
        # 3. resume to completion
        app._set_running(True); app._launch(app.settings(), "run"); pump(app, done, 240)
        assert app.v_status.get() == "Finished.", app.v_status.get()
        assert app.archive and os.path.exists(app.archive), app.archive
        assert app.pb["value"] == 1000
        z = zipfile.ZipFile(app.archive); assert z.testzip() is None
        for rel in ("README.md", "src/main.py", "src/util/helpers.js", "a/b/c/deep.sh"):
            src = open(os.path.join(repo, rel), "rb").read(); assert z.read("demo/" + rel) == src, rel
        print("GUI resume + ZIP OK ->", os.path.basename(app.archive))
        app.destroy()
    try:
        gui.run_gui(drive)
        print("GUI TEST PASS")
    finally:
        open(os.path.join(fake, "consumer_stop"), "w").close(); time.sleep(0.3); cons.kill()
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
