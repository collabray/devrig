#!/usr/bin/env python3
"""Virtual workstation: decodes 8-byte HID reports from $FAKEROOT/hidg0 into a text buffer
of the 'open' file (auto-saved on every change, like VS Code afterDelay)."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "android"))
import hid_typer as H
R = os.environ["FAKEROOT"]
dev, cur_f = R + "/hidg0", R + "/vscode_current"
pos, prev, cur, buf, sel = 0, set(), None, bytearray(), False
def save():
    open(cur, "wb").write(bytes(buf))
while not os.path.exists(R + "/consumer_stop"):
    if not os.path.exists(dev): time.sleep(0.02); continue
    with open(dev, "rb") as f:
        f.seek(pos); data = f.read()
    n = len(data) // 8
    for i in range(n):
        rep = data[i*8:(i+1)*8]; pos += 8
        mod, keys = H.decode_report(rep)
        new = set(keys) - prev; prev = set(keys)
        for k in new:
            try: path = open(cur_f).read().strip()
            except OSError: path = None
            if path and path != cur:
                cur = path; buf = bytearray(open(cur, "rb").read()); sel = False
            if not cur: continue
            ctrl = mod & 0x11
            if ctrl:
                if k == H.KEYCODES["a"]: sel = True
                elif k == H.KEYCODES["s"]: save()
                continue
            if k == H.KEYCODES["delete"]:
                if sel: buf = bytearray(); sel = False; save()
                continue
            if k == H.KEYCODES["backspace"]:
                if buf: buf.pop(); save()
                continue
            ch = H.REVERSE_MAP.get((mod & 0x22 and 0x02 or 0, k))
            if ch is not None:
                if sel: buf = bytearray(); sel = False
                buf += ch.encode(); save()
    time.sleep(0.01)
