<#
.SYNOPSIS
  DevRig-HID Windows bootstrap: checks/installs dependencies, pushes the rig scripts to the
  Android board, creates the USB HID gadget and verifies that Windows sees the keyboard.

.PARAMETER AutoInstall   Install missing Git / VS Code / Python / adb with winget.
.PARAMETER SoftwareOnly  No hardware: only check/install the PC tools (use with --backend software).
.PARAMETER Serial        adb serial when more than one device is attached.
.PARAMETER SkipGadget    Do not (re)configure the USB gadget (push + checks only).
.PARAMETER ForceGadget   Re-run gadget setup even if /dev/hidg0 already exists.
.PARAMETER RunTests      Run the offline unit tests and the host-side checks.
.PARAMETER UsbVid / UsbPid    USB IDs used only if the board has no Android-managed gadget (hex, e.g. 0x18d1).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1 -AutoInstall -RunTests
#>
[CmdletBinding()]
param(
    [switch]$AutoInstall,
    [switch]$SoftwareOnly,
    [string]$Serial = "",
    [switch]$SkipGadget,
    [switch]$ForceGadget,
    [switch]$RunTests,
    [string]$UsbVid = "0x18d1",
    [string]$UsbPid = "0x4ee7"
)

$ErrorActionPreference = "Stop"
$Root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$RemoteDir  = "/data/local/tmp/devrig"
$StageDir   = "/data/local/tmp/devrig_stage"
$script:Failed   = $false
$script:AdbExit  = 0

function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [ OK ] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [WARN] $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "    [FAIL] $m" -ForegroundColor Red; $script:Failed = $true }
function Die($m)  { Write-Host "    [FAIL] $m" -ForegroundColor Red; exit 1 }

function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Winget-Install($id, $label) {
    if (-not (Have "winget")) { Die "winget is not available; install $label manually." }
    Write-Host "    installing $label via winget ..."
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
        & winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
    # winget also returns non-zero for "already installed / no update"; the caller re-checks anyway.
    if ($code -ne 0) { Warn "winget exited with code $code for $label (verifying below)." }
    Refresh-Path
}

# adb wrappers ---------------------------------------------------------------------------------
# NOTE: the wrapper functions are deliberately NOT named "adb" - PowerShell resolves functions
# before executables, so a function called Adb that runs `adb` would call itself forever.
function Invoke-AdbCore([string[]]$a) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
        $out = & adb.exe @a 2>&1
        $script:AdbExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
    return (($out | ForEach-Object { "$_" }) -join "`n")
}
function AdbHost {
    # no -s serial (server-level commands)
    return (Invoke-AdbCore ([string[]]$args))
}
function AdbRun {
    $a = @()
    if ($Serial -ne "") { $a += @("-s", $Serial) }
    $a += $args
    return (Invoke-AdbCore ([string[]]$a))
}
function AdbSu([string]$cmd) {
    # single-quote the payload so the device shell hands it to su intact
    $q = "'" + $cmd.Replace("'", "'\''") + "'"
    return (AdbRun shell "su -c $q")
}
function AdbPush([string]$local, [string]$remote) {
    $out = AdbRun push $local $remote
    if ($script:AdbExit -ne 0) { Die "adb push failed for $local`n$out" }
}

# 1. Host tooling ------------------------------------------------------------------------------
Step "Checking host tooling"
if ($PSVersionTable.PSVersion.Major -lt 5) { Die "PowerShell 5.1+ is required." }

function Find-Python {
    foreach ($c in @("py", "python", "python3")) {
        if (-not (Have $c)) { continue }
        $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        try {
            $v = if ($c -eq "py") { & $c -3 --version 2>&1 } else { & $c --version 2>&1 }
        } catch { $v = "" } finally { $ErrorActionPreference = $old }
        # the Microsoft Store "python" stub prints a help message instead of a version -> no match
        if ("$v" -match "Python (\d+)\.(\d+)" -and [int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 8) { return $c }
    }
    return $null
}

$py = Find-Python
if (-not $py -and $AutoInstall) { Winget-Install "Python.Python.3.12" "Python 3.12"; $py = Find-Python }
if (-not $py) { Die "Python 3.8+ not found (use -AutoInstall, or install from python.org and reopen the terminal)." }
Ok "Python launcher: $py"

if (-not (Have "git")) { if ($AutoInstall) { Winget-Install "Git.Git" "Git" } }
if (Have "git") { Ok ("git: " + (& git --version)) } else { Die "git not found (use -AutoInstall, then reopen the terminal)." }

if (-not (Have "code")) { if ($AutoInstall) { Winget-Install "Microsoft.VisualStudioCode" "Visual Studio Code" } }
if (Have "code") { Ok "VS Code CLI ('code') found" } else { Die "VS Code CLI 'code' not on PATH (reinstall VS Code with 'Add to PATH' enabled)." }

if (-not $SoftwareOnly) {
    if (-not (Have "adb.exe")) { if ($AutoInstall) { Winget-Install "Google.PlatformTools" "Android platform-tools (adb)" } }
    if (Have "adb.exe") {
        Ok ("adb: " + ((AdbHost version) -split "\r?\n" | Select-Object -First 1))
    } else { Die "adb not found (use -AutoInstall, then reopen the terminal if PATH did not refresh)." }
}

# keyboard layout (HID scancodes assume US QWERTY)
try {
    $tips = (Get-WinUserLanguageList)[0].InputMethodTips
    if ($tips -contains "0409:00000409") { Ok "primary input method is English (US)" }
    else { Warn "primary input method is '$($tips -join ',')'. Switch to English (US) before typing, or the characters will be wrong." }
} catch { Warn "could not read the input language list" }

if ($SoftwareOnly) { Warn "software-only mode: skipping the Android board steps" } else {

# 2. Device -------------------------------------------------------------------------------------
Step "Looking for the Android development board"
AdbHost start-server | Out-Null
$lines = @((AdbHost devices) -split "\r?\n" |
           ForEach-Object { $_.Trim() } |
           Where-Object { $_ -match "\s(device|unauthorized|offline|recovery|sideload)$" })
if ($lines.Count -eq 0) { Die "No device attached. Enable USB debugging on the board and plug it in (data cable)." }
if ($lines | Where-Object { $_ -match "unauthorized" }) { Die "Device is unauthorized: accept the RSA fingerprint prompt on the board." }
$devs = @($lines | Where-Object { $_ -match "\sdevice$" })
if ($devs.Count -eq 0) { Die "No device in 'device' state: $($lines -join '; ')" }
if ($devs.Count -gt 1 -and $Serial -eq "") { Die "Several devices attached; re-run with -Serial <id>.`n$($devs -join "`n")" }
Ok ("device: " + $devs[0])

$id = AdbSu "id"
if ($id -match "uid=0") { Ok "root (su) works" } else { Die "su is not available/granted on the board (output: $id). Root is required for /dev/hidg0." }

# 3. Push scripts ------------------------------------------------------------------------------
Step "Pushing rig scripts to $RemoteDir"
# Push into a staging dir as the adb shell user, then copy into a root-owned, non-world-writable
# directory so no other process can swap the scripts that later run as root.
AdbSu "rm -rf $StageDir && mkdir -p $StageDir && chmod 755 $StageDir" | Out-Null
AdbPush "$Root\android\hid_typer.py"        "$StageDir/hid_typer.py"
AdbPush "$Root\android\setup_hid_gadget.sh" "$StageDir/setup_hid_gadget.sh"
AdbSu "mkdir -p $RemoteDir && cp $StageDir/hid_typer.py $StageDir/setup_hid_gadget.sh $RemoteDir/ && rm -rf $StageDir" | Out-Null
# repair CRLF line endings if git/Windows converted the files
AdbSu "sed -i 's/\r`$//' $RemoteDir/setup_hid_gadget.sh $RemoteDir/hid_typer.py" | Out-Null
AdbSu "chown root:root $RemoteDir $RemoteDir/hid_typer.py $RemoteDir/setup_hid_gadget.sh 2>/dev/null; chmod 755 $RemoteDir $RemoteDir/hid_typer.py $RemoteDir/setup_hid_gadget.sh" | Out-Null
$chk = AdbSu "ls $RemoteDir/hid_typer.py $RemoteDir/setup_hid_gadget.sh 2>&1"
if ($chk -match "No such file") { Die "Scripts are missing on the board after the push:`n$chk" }
Ok "scripts pushed"

# 4. Python on the board ------------------------------------------------------------------------
Step "Locating Python 3 on the board"
$termuxEnv = "export PATH=/data/data/com.termux/files/usr/bin:`$PATH LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib:`$LD_LIBRARY_PATH; "
$pyDev = $null
foreach ($cand in @(@("python3", ""), @("python", ""), @("/data/data/com.termux/files/usr/bin/python3", $termuxEnv))) {
    $r = AdbSu ($cand[1] + $cand[0] + " --version 2>&1")
    if ($r -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 6) { $pyDev = $cand; Ok "$($cand[0]) -> $r"; break }
}
if (-not $pyDev) { Die "No Python 3 on the board. Install Termux and run 'pkg install python', or push a python3 binary." }
$st = AdbSu ($pyDev[1] + $pyDev[0] + " $RemoteDir/hid_typer.py selftest 2>&1")
Write-Host ($st -replace "(?m)^", "      ")
if ($st -match "SELFTEST OK") { Ok "board-side scancode/pacing self-test passed" } else { Bad "board-side self-test failed" }

# 5. USB gadget ----------------------------------------------------------------------------------
if (-not $SkipGadget) {
    Step "Configuring the USB composite gadget (ADB + HID keyboard)"
    $state = AdbSu "ls /dev/hidg0 2>&1; cat /sys/class/udc/*/state 2>&1; true"
    if (($state -match "/dev/hidg0") -and ($state -notmatch "No such file") -and ($state -match "configured") -and -not $ForceGadget) {
        Ok "HID gadget already active"
    } else {
        Warn "The USB link will drop for a few seconds while the gadget is re-bound."
        AdbSu "DEVRIG_VID=$UsbVid DEVRIG_PID=$UsbPid sh $RemoteDir/setup_hid_gadget.sh" | Out-Null
        $result = $null; $log = ""
        for ($i = 0; $i -lt 60 -and -not $result; $i++) {
            Start-Sleep -Seconds 2
            try { $log = AdbSu "cat $RemoteDir/gadget_setup.log 2>/dev/null; true" } catch { continue }
            foreach ($l in ($log -split "\r?\n")) { if ($l -match "^DEVRIG_RESULT=") { $result = $l.Trim() } }
        }
        if (-not $result) { Die "Gadget setup did not report a result.`n$log" }
        if ($result -match "FAIL") { Die "Gadget setup failed: $result`n$log" }
        Ok $result
        Start-Sleep -Seconds 6
    }
    $state = AdbSu "ls /dev/hidg0 2>&1; cat /sys/class/udc/*/state 2>&1; true"
    if ($state -match "configured") { Ok "UDC state: configured (the PC enumerated the board)" }
    else { Bad "UDC is not 'configured' - use a data-capable cable and make sure this PC is the USB host." }
    if ($state -match "No such file") { Bad "/dev/hidg0 is missing (kernel without CONFIG_USB_CONFIGFS_F_HID?)" } else { Ok "/dev/hidg0 present" }

    try {
        $vid = ($UsbVid -replace "^0[xX]", "").PadLeft(4, "0").ToUpper()
        $hid = Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object {
            $_.InstanceId -match "VID_$vid" -and $_.Class -in @("Keyboard", "HIDClass", "USB")
        }
        if ($hid) {
            Ok ("Windows sees: " + (($hid | Select-Object -First 3 | ForEach-Object { $_.FriendlyName + " [" + $_.InstanceId + "]" }) -join "; "))
        } else {
            Warn "No device with VID_$vid visible to Windows yet. If the board uses an Android-managed gadget it may use a different VID; check: Get-PnpDevice -PresentOnly -Class Keyboard"
        }
    } catch { Warn "Get-PnpDevice unavailable; skipping Windows-side device check." }
} else { Warn "gadget setup skipped (-SkipGadget)" }

}   # end of Android-board steps

# 6. Host self-tests ------------------------------------------------------------------------------
if ($RunTests) {
    Step "Running host-side tests"
    $pyArgs = if ($py -eq "py") { @("-3") } else { @() }
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
        & $py @pyArgs "$Root\tests\test_core.py"
        if ($LASTEXITCODE -eq 0) { Ok "unit tests passed" } else { Bad "unit tests failed" }
        $be = if ($SoftwareOnly) { "software" } else { "android" }
        & $py @pyArgs "$Root\host\orchestrator.py" --check-only --backend $be
        if ($LASTEXITCODE -eq 0) { Ok "orchestrator --check-only passed" } else { Bad "orchestrator --check-only failed" }
    } finally { $ErrorActionPreference = $old }
}

Write-Host ""
if ($script:Failed) { Write-Host "Setup finished WITH ERRORS - fix the [FAIL] items above." -ForegroundColor Red; exit 1 }
Write-Host "DevRig-HID is ready." -ForegroundColor Green
Write-Host "Example:"
if ($SoftwareOnly) { $bk = "software" } else { $bk = "android" }
Write-Host "  $py host\orchestrator.py https://github.com/OWNER/REPO C:\work\REPO-out --backend $bk"