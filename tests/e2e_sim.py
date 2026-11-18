#!/usr/bin/env python3
"""End-to-end simulation: real orchestrator + real hid_typer daemon + fake adb/code/keyboard.
Includes a hard kill mid-run followed by a resume.   Usage: python tests/e2e_sim.py"""
import hashlib, os, shutil, signal, subprocess, sys, tempfile, time, zipfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
SIM = os.path.join(HERE, "sim")

def sh(*a, **k): return subprocess.run(a, check=True, **k)

def make_repo(path):
    os.makedirs(path)
    def w(rel, data, mode="w"):
        p = os.path.join(path, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, mode + ("b" if isinstance(data, bytes) else ""), **({} if isinstance(data, bytes) else {"newline": ""})).write(data)
    w("README.md", "# Demo\n\nSome `code` and *text* -- (parens) [brackets] {braces} \"quotes\" 'single'.\n")
    w("src/main.py", "import os\n\n\ndef main():\n\tif os.environ.get('X') == \"1\":\n\t\tprint({'a': [1, 2, 3], 'b': (4, 5)})  # ok\n    \n\treturn 0 <= 1 and 2 >= 1 || ~3 ^ 4 & 5 | 6\n\n\nif __name__ == '__main__':\n    main()\n")
    w("src/util/helpers.js", "const f = (x) => {\n  return `${x}` + '\\n' + \"\\t\" + x / 2 % 3;\n};\nmodule.exports = { f };\n" * 15)
    w("src/__init__.py", "")
    w("docs/notes.txt", "caf\u00e9 \u2014 unicode stays verbatim\n")
    w("docs/crlf.txt", "line one\r\nline two\r\nno final newline")
    w("assets/blob.bin", bytes(range(256)) * 4, "w")
    w("package-lock.json", '{"lock": true}\n')
    w("a/b/c/deep.sh", "#!/bin/sh\necho \"deep $HOME\" | tr a-z A-Z\n")
    for c in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        sh("git", "-C", path, *c)

def main(backend="android"):
    tmp = tempfile.mkdtemp(prefix="devrig_e2e_")
    fake = os.path.join(tmp, "fake"); os.makedirs(fake)
    repo = os.path.join(tmp, "upstream"); make_repo(repo)
    ws = os.path.join(tmp, "out", "demo"); os.makedirs(os.path.dirname(ws))
    env = dict(os.environ, FAKEROOT=fake, PATH=os.path.join(SIM, "bin") + os.pathsep + os.environ["PATH"])
    open(os.path.join(fake, "hidg0"), "wb").close()
    os.makedirs(os.path.join(fake, "udc", "fake.usb"), exist_ok=True)
    open(os.path.join(fake, "udc", "fake.usb", "state"), "w").write("configured\n")
    cons = subprocess.Popen([sys.executable, os.path.join(SIM, "consumer.py")], env=env)
    cmd = [sys.executable, os.path.join(ROOT, "host", "orchestrator.py"), "file://" + repo, ws,
           "--wpm", "2500", "--pause-min", "0.05", "--pause-max", "0.1", "--pause-mean-interval", "1",
           "--hold-ms", "4", "--timing-scale", "0.02", "--settle", "2", "--file-gap-min", "0", "--file-gap-max", "0.05",
           "--typer-extra", "--allow-create --no-leds", "--android-python", "python3",
           "--backend", backend, "--software-sink", os.path.join(fake, "hidg0")]
    try:
        # run 1: hard-kill part-way through
        p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        t0 = time.time(); lines = []
        for line in p.stdout:
            lines.append(line)
            if "typing src/util/helpers.js" in line or time.time() - t0 > 60: break
        time.sleep(0.8); p.send_signal(signal.SIGKILL); p.wait()
        print("--- run 1 killed ---"); print("".join(lines[-6:]))
        r = subprocess.run(cmd + ["--status"], env=env, capture_output=True, text=True); print(r.stdout)
        # run 2: resume to completion
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        print(r.stdout[-2500:], r.stderr[-800:])
        assert r.returncode == 0, "resume run failed"
        assert "resuming run #2" in r.stdout
        zips = [f for f in os.listdir(os.path.dirname(ws)) if f.endswith(".zip")]
        assert len(zips) == 1, zips
        z = zipfile.ZipFile(os.path.join(os.path.dirname(ws), zips[0])); assert z.testzip() is None
        exp = {"README.md", "src/main.py", "src/util/helpers.js", "src/__init__.py", "docs/notes.txt", "docs/crlf.txt",
               "assets/blob.bin", "package-lock.json", "a/b/c/deep.sh"}
        got = {n[len("demo/"):] for n in z.namelist() if not n.endswith("/")}
        assert got == exp, (got ^ exp)
        for rel in exp:
            src = open(os.path.join(repo, rel), "rb").read()
            out = z.read("demo/" + rel)
            if rel == "docs/crlf.txt": src = src.replace(b"\r\n", b"\n")   # typed files are LF-normalised
            assert src == out, "content mismatch: " + rel
        print("E2E PASS [%s]: %d files byte-identical (kill+resume, zip verified)" % (backend, len(exp)))
    finally:
        open(os.path.join(fake, "consumer_stop"), "w").close(); time.sleep(0.2); cons.kill()
        subprocess.run(["pkill", "-f", "hid_type[r].py"])
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "android")
