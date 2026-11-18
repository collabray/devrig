import json, os, sys, tempfile, unittest, zipfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "host"), os.path.join(ROOT, "android")]
import hid_typer as H, state_manager as SM, packager as P, repo_planner as RP, orchestrator as O

class Scancodes(unittest.TestCase):
    def test_every_printable_roundtrips(self):
        for c in [chr(i) for i in range(32, 127)] + ["\n", "\t"]:
            m, k = H.CHAR_MAP[c]; rep = H.build_report(m, (k,))
            self.assertEqual(len(rep), 8)
            mm, kk = H.decode_report(rep); self.assertEqual(H.REVERSE_MAP[(mm, kk[0])], c)
    def test_known_codes(self):
        self.assertEqual(H.CHAR_MAP["A"], (2, 4)); self.assertEqual(H.CHAR_MAP["{"], (2, 0x2F))
        self.assertEqual(H.CHAR_MAP["\n"], (0, 0x28)); self.assertEqual(H.CHAR_MAP["0"], (0, 0x27))
    def test_untypeable(self):
        self.assertEqual(H.untypeable("a\u00e9b\x0c"), [(1, "\u00e9"), (3, "\x0c")])

class Pacing(unittest.TestCase):
    def test_pauses_in_range_and_poisson_rate(self):
        pc = H.Pacing(wpm=70, pause_mean_interval=60, seed=1); n = 0
        for _ in range(20000):
            pc.account(0.17)
            if pc.pause_due(): self.assertTrue(3 <= pc.draw_pause() <= 7); n += 1
        self.assertTrue(0.6 * 56 <= n <= 1.5 * 56, n)
    def test_intervals_bounded(self):
        pc = H.Pacing(wpm=70, seed=2)
        for ch in "Hello, World!\n    x = {}\n" * 50:
            t = pc.char_interval("a", ch, False); self.assertTrue(pc.lo <= t <= pc.hi)

class State(unittest.TestCase):
    def test_atomic_backup_recovery(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "state_manager.json")
        s = SM.StateManager(p); s.init_new("u", d, None)
        s.set_plan([RP._entry("a.txt", "type", 3, "x", "")]); s.update_entry("a.txt", status="done")
        s.save()
        open(p, "w").write("{corrupt")            # simulated torn write
        s2 = SM.StateManager(p); self.assertTrue(s2.load())
        self.assertIn(s2.entry("a.txt")["status"], ("pending", "done"))
    def test_lock(self):
        d = tempfile.mkdtemp(); a = SM.ProcessLock(os.path.join(d, "l")); a.acquire()
        b = SM.ProcessLock(os.path.join(d, "l"))
        with self.assertRaises(SM.LockError): b.acquire()
        a.release(); b.acquire(); b.release()

class Planner(unittest.TestCase):
    cfg = {"copy_globs": RP.DEFAULT_COPY_GLOBS, "max_file_bytes": 1000, "non_ascii": "copy", "include": [], "exclude": []}
    def test_modes(self):
        f = RP.prepare_content
        self.assertEqual(f("a.py", b"x=1\r\n", self.cfg)[0:2], ("type", b"x=1\n"))
        self.assertEqual(f("a.bin", b"\x00\x01", self.cfg)[0], "copy")
        self.assertEqual(f("a.txt", "caf\u00e9".encode(), self.cfg)[0], "copy")
        self.assertEqual(f("yarn.lock", b"x", self.cfg)[0], "copy")
        self.assertEqual(f("big.txt", b"a" * 2000, self.cfg)[0], "copy")
        self.assertEqual(f("e", b"", self.cfg)[0], "empty")
        c = dict(self.cfg, non_ascii="transliterate")
        self.assertEqual(f("a.txt", "caf\u00e9 \u2014 \u201cq\u201d".encode(), c)[1], b'cafe -- "q"')
    def test_url(self):
        self.assertEqual(RP.normalize_repo_url("https://github.com/o/r/tree/dev"), ("https://github.com/o/r.git", "dev"))
        self.assertEqual(RP.normalize_repo_url("https://github.com/o/r.git/"), ("https://github.com/o/r.git", None))

class Misc(unittest.TestCase):
    def test_common_prefix(self):
        self.assertEqual(O.common_prefix_len(b"abcdef", b"abcxef"), 3)
        self.assertEqual(O.common_prefix_len(b"abc", b"abcdef"), 3)
        self.assertEqual(O.common_prefix_len(b"", b"abc"), 0)

class Pack(unittest.TestCase):
    def test_archive(self):
        d = tempfile.mkdtemp(); ws = os.path.join(d, "proj"); os.makedirs(ws + "/s/e")
        open(ws + "/a.txt", "w").write("hi"); open(ws + "/s/b.txt", "w").write("yo")
        info = P.create_archive(ws, os.path.join(d, "out"))
        z = zipfile.ZipFile(info["archive"]); self.assertIsNone(z.testzip())
        self.assertEqual(sorted(n for n in z.namelist() if not n.endswith("/")), ["proj/a.txt", "proj/s/b.txt"])
        self.assertIn("proj/s/e/", z.namelist())
        self.assertTrue(os.path.exists(info["archive"] + ".sha256"))
        self.assertFalse(os.path.exists(info["archive"] + ".part"))

if __name__ == "__main__":
    unittest.main()
