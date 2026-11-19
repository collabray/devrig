@echo off
rem DevRig-HID launcher: double-click to open the window.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0host\gui.py"
  exit /b
)
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 "%~dp0host\gui.py"
  exit /b
)
echo Python 3 was not found. Install it from python.org (tick "Add python.exe to PATH"), then try again.
pause
