# DevRig-HID

Automated physical input-injection rig for software-engineering workspaces.

A **Windows host** clones a GitHub repository, builds a file plan, and — for every file — creates the
directories and file, opens it in an isolated **VS Code**, and has it typed by an **Android
development board acting as a real USB keyboard** (Linux ConfigFS gadget, `/dev/hidg0`). Because the
input is genuine hardware HID, Windows registers it natively. When everything is verified, the
workspace is compressed into a timestamped ZIP.

```
 GitHub ──git clone──▶ Windows host ──ADB (USB)──▶ Android board (root)
                        orchestrator.py            hid_typer.py ─▶ /dev/hidg0
                             ▲                                        │ USB HID reports
                             └──── VS Code (same PC) ◀── keystrokes ──┘
```

The board is one composite USB device: **ADB** (control channel) + **HID keyboard** (typing).
One cable to the PC does both.

## Two input backends (switch with one flag)

| `--backend` | What types the keys | Needs |
|---|---|---|
| `software` (default) | this PC, using Windows `SendInput` scancodes | nothing but Windows, Git, VS Code, Python |
| `android` | a physical USB keyboard (rooted Android board, `/dev/hidg0`) | the board, `adb`, root |

Both run the **same engine** (cadence, Poisson pauses, combos, focus guard, verify, resume, ZIP), so
behaviour is identical. Start now with software; when hardware arrives just add `--backend android`
(or set it once: `setx DEVRIG_BACKEND android`). The ledger is backend-independent, so a half-finished
run can be resumed with the other backend. Software mode is plain automation: apps can tell injected
input from a physical keyboard, and it cannot type into elevated (admin) windows from a non-admin console.

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 -AutoInstall -SoftwareOnly -RunTests
python host\orchestrator.py https://github.com/OWNER/REPO C:\work\REPO-out            # software
python host\orchestrator.py https://github.com/OWNER/REPO C:\work\REPO-out --backend android
```

New hardware later (e.g. a Raspberry Pi Pico) is added as one more session class in
`host/backends.py` exposing `start() / request() / close() / alive`; nothing else changes.

## Contents

| Path | Purpose |
|---|---|
| `host/orchestrator.py` | CLI, preflight guardrails, plan, typing loop, recovery, audit, packaging |
| `host/state_manager.py` | Atomic `state_manager.json` ledger + process lock |
| `host/repo_planner.py` | Clone + deterministic typing plan |
| `host/backends.py` | Backend layer: `software` (Windows SendInput) session; hardware plug-in point |
| `host/typer_session.py`, `host/adb_bridge.py` | ADB transport and the JSON-lines session with the daemon |
| `host/vscode_env.py`, `host/win_focus.py` | Isolated VS Code settings; foreground/keyboard-layout guard |
| `host/packager.py` | Lock check → ZIP → CRC + per-file SHA-256 verify → atomic rename |
| `android/setup_hid_gadget.sh` | ConfigFS gadget (`--teardown`, `--status`) |
| `android/hid_typer.py` | Scancode engine, cadence/pause engine, hardware daemon |
| `setup_windows.ps1` | Dependency + rig bootstrap |
| `tests/` | Unit tests and a full end-to-end simulation |

Host code uses only the Python standard library. The board needs Python 3.6+ (Termux works).

## Requirements

**Windows host:** Windows 10/11, Python 3.8+, Git, VS Code (`code` on PATH), Android platform-tools
(`adb`). **The active keyboard layout must be English (US)** — HID sends physical key positions.

**Android board:** rooted (`su`), USB debugging on, kernel with `CONFIG_USB_CONFIGFS` and
`CONFIG_USB_CONFIGFS_F_HID`, a USB port in *device/peripheral* mode, Python 3. Boards that qualify
include rooted phones with HID-enabled kernels and dev boards running Android. If `ls
/sys/kernel/config/usb_gadget` or `mkdir …/functions/hid.usb0` fails, your kernel lacks support.

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 -AutoInstall -RunTests
python host\orchestrator.py https://github.com/OWNER/REPO C:\work\REPO-out
```

`setup_windows.ps1` installs missing tools (with `-AutoInstall`), checks root, pushes scripts to
`/data/local/tmp/devrig`, finds Python on the board, runs the board self-test, creates the USB
gadget, and confirms the PC enumerated it. The USB link **drops for a few seconds** during gadget
binding; this is expected and handled.

Preview without typing anything: add `--dry-run` (prints the plan and an estimated duration).
Check the rig only: `python host\orchestrator.py --check-only`.

## How a file gets typed

1. Create directories and an empty file (recorded in the ledger).
2. `code --reuse-window --goto file:line:col` opens it in an **isolated VS Code instance**
   (`--user-data-dir` under the control folder, extensions disabled).
3. The daemon sends `Ctrl+1`, `Ctrl+End`, then types. Each key is an 8-byte press report, a
   randomized hold (~68 ms ± jitter), and an 8-byte release report; shifted symbols press Shift first.
4. `Ctrl+S`, then the orchestrator compares the file on disk with the plan (exact bytes). Mismatch →
   retried (up to `--max-file-attempts`); then `Ctrl+W`.

Directories and the empty file are created by the host (so the path is always exact); *content entry*
is done through the keyboard. The isolated VS Code settings disable auto-closing brackets/quotes,
auto-indent, suggestions, formatters, Emmet, whitespace trimming and CRLF conversion so typed text is
literal, and enable 500 ms auto-save so disk mirrors the editor.

### Cadence

Baseline `--wpm 70` (≈5.8 chars/s, effective ≈5.2 with word/line rhythm). Intervals are log-normal
with an AR(1) rhythm term and per-key factors (indentation fast, symbols/capitals slower, a short think
at line starts). **Distraction pauses** form a Poisson process: the gap to the next pause is
exponentially distributed with mean `--pause-mean-interval` (default 150 s of typing), each pause is
uniform 3–7 s (`--pause-min/--pause-max`). Unpredictable by construction. Typing a large repo at human
speed is inherently slow — use `--dry-run` for the estimate.

## Resilience and resume

`<workspace-parent>\.devrig_<workspace>\state_manager.json` (plus `.bak`) records phase, plan, per-file
status and byte offsets, directories and the archive. Writes are atomic. To recover from a PC crash,
power loss, thermal shutdown or ADB drop, **run the same command again**:

* the ledger is loaded (falls back to `.bak` if torn), and the repo clone is re-pinned to the recorded commit;
* completed files are re-hashed against the plan; damaged ones are re-queued;
* for a half-typed file the **bytes on disk are the source of truth**: if they are an exact prefix of the
  expected content, typing continues from that offset (cursor placed at the end) — no duplicated code;
  if they diverge, the file is cleared inside the IDE and retyped;
* during a run, ADB loss / daemon death triggers reconnect with backoff (`--max-reconnects`,
  `--reconnect-timeout`), the gadget is re-verified (and re-created after a board reboot), then work resumes.

Safety interlocks: the daemon has a **watchdog** (stops and releases all keys if the host heartbeat stops
or stdin closes); the host aborts typing within ~1 s if **VS Code loses foreground focus** and resumes
after focus returns; **CapsLock** state is read from the keyboard LED reports and cleared; a lock file
prevents two orchestrators using one rig; unmappable characters are rejected before any key is sent.

Delete the ledger with `--reset` to start over. `--status` prints a summary.

## What is typed, what is copied

| Mode | Files |
|---|---|
| typed | UTF-8 text made only of printable ASCII, LF and TAB (CRLF is normalised to LF) |
| copied verbatim | binaries, files > `--max-file-bytes` (1 MiB), lockfiles/`*.min.js`/`*.map` (`--copy-glob` to add), and files with characters that have no US scancode (`--non-ascii copy`, default) |
| alternatives | `--non-ascii transliterate` (curly quotes → `"`, é → e, …) or `error` |
| skipped | symlinks and submodules |

Filter with `--include` / `--exclude` (fnmatch globs). Everything is listed by `--dry-run`.

## Packaging

After the audit (every file byte-for-byte equals the plan), the rig closes editors and the VS Code
window (only if VS Code has focus), `packager.py` verifies no file handle is held, writes
`<name>_YYYYMMDD-HHMMSS.zip.part`, re-reads it (CRC + SHA-256 of every member), and atomically renames it.
Sidecars: `.zip.sha256` and `.zip.manifest.json`. Output goes next to the workspace (`--archive-dir`
to change; must be outside it). Standalone: `python host\packager.py <folder>`.

## Command reference (selected)

```
orchestrator.py REPO_URL WORKSPACE
  --branch B  --serial ID  --android-python PATH  --hid-device /dev/hidg0
  --wpm 70  --pause-min 3  --pause-max 7  --pause-mean-interval 150  --hold-ms 68
  --non-ascii {copy,transliterate,error}  --max-file-bytes N  --copy-glob G  --include G  --exclude G
  --no-package  --archive-dir D  --archive-name N  --keep-vscode-open
  --dry-run  --check-only  --status  --reset  --teardown-gadget  --force-gadget-setup  -v
```

Gadget script on the board: `su -c 'sh /data/local/tmp/devrig/setup_hid_gadget.sh [--status|--teardown|--permissive|--udc NAME]'`.
Typing smoke test: `su -c 'python3 /data/local/tmp/devrig/hid_typer.py demo --text "hello\n"'` (focus Notepad first).

## Safety while it types

* **Emergency stop: press F12 (or Pause/Break) on the physical keyboard.** Typing halts within half a
  second; progress is saved and the same command resumes.
* Do not click or type while it runs. Before every file the tool types **one probe character** and checks
  that it reached the file before typing the rest; if the editor does not own the keyboard it re-focuses
  (it clicks the editor area) and retries instead of typing into menus or panels.
* The tool only types while the foreground window is *its own* VS Code window (title contains the
  workspace folder name); other VS Code windows are ignored. Close them anyway to be safe.
* The isolated VS Code hides the AI/chat panel, command centre, menu bar and activity bar.
* A file whose on-disk content disagrees with the plan is repaired by closing all editors and trimming
  the file to its last correct byte - the tool never uses Ctrl+A / Delete.

## Speed

Typing 450 KB at 70 WPM takes ~23 hours. For a trial use a small repo, raise `--wpm 250`, or make big
files copy instead of type: `--max-file-bytes 20000`. `--dry-run` prints the estimate first.

## Troubleshooting

**"It does not create folders."** Folders are created on demand when the first file inside them is
reached (log line `mkdir <folder>`). A repo whose files are all in the top level has none to create.
Folders are made by the host, not by typing in VS Code's explorer.

**`PermissionError ... state_manager.json`.** Antivirus/indexer briefly locking the ledger. Fixed: writes
now retry for ~15 s and a failed progress save never stops the run. Excluding the `.devrig_*` folder in
Windows Security makes it smoother.


**`adb devices` empty / unauthorized.** Use a data cable; enable USB debugging; accept the RSA prompt;
`adb kill-server && adb start-server`. Install the Google USB driver if Windows shows an unknown ADB
interface. After gadget binding the serial normally stays the same; if it changes, omit `--serial`.

**No ADB over USB after setup.** Some Android builds' USB HAL rewrites the gadget on mode changes. Use
ADB over Wi‑Fi instead (`adb tcpip 5555`, `adb connect IP:5555`, `--serial IP:5555`); it is unaffected by
USB re-binding. Re-run the gadget script after toggling USB mode (the HAL removes the HID link).

**`su` fails / “root required”.** Grant shell root in Magisk/SuperSU. The orchestrator tries `su -c` and `su 0`.

**`mkdir …/hid.usb0` fails.** Kernel lacks HID gadget support — needs a different kernel/board.

**`/dev/hidg0` exists but Windows shows no keyboard / UDC not `configured`.** Wrong port or charge-only
cable; the board is in host mode; or the PC is not the USB host. Check `setup_hid_gadget.sh --status`.
Permission denied on `/dev/hidg0`: SELinux — re-run gadget setup with `--permissive`.

**Wrong characters (`"` ↔ `@`, `z`/`y` swapped).** Windows layout is not US. Switch to English (US).

**Typing starts in the wrong window.** The guard waits for VS Code focus; keep the PC unattended and
disable focus-stealing apps. Windows only enforces the guard on the host the orchestrator runs on.

**Autocomplete/auto-close still alters text.** The orchestrator's VS Code instance ignores user settings;
make sure you did not open the workspace in another VS Code window. Verification will catch and retry.

**Calibration.** `--settle` (seconds after opening a file; raise on slow PCs), `--vscode-startup`,
`--wpm`. Run `hid_typer.py selftest` on the board to verify tables and pacing statistics.

**Logs.** Host: `.devrig_<workspace>\devrig.log` (use `-v` for the console). Board:
`/data/local/tmp/devrig/hid_typer.log`, `gadget_setup.log`.

## Tests

```
python tests\test_core.py      # scancodes, pacing, ledger recovery, planner, packager
python tests\e2e_sim.py        # real orchestrator + daemon against a simulated adb/VS Code/keyboard,
                               # including SIGKILL mid-file and resume (POSIX)
```

## Responsible use

Use the rig only on machines and accounts you own or administer, on code you are licensed to copy, and
never to misrepresent authorship or evade monitoring in exams, assessments, or employer systems.
