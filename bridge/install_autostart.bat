@echo off
REM ============================================================
REM  Kronos QMT Bridge - AUTO START at login (run once)
REM
REM  Copies a launcher into the user's Startup folder, so the
REM  bridge starts automatically every time you log in via RDP.
REM  (QMT client itself also needs to be running & logged in -
REM   enable auto-login inside the QMT client settings.)
REM
REM  Edit USERDATA / ACCOUNT / TOKEN first, same as
REM  start_bridge.bat, then double-click this file.
REM ============================================================

set USERDATA=D:\QMT\userdata_mini
set ACCOUNT=40735103
set TOKEN=Vk9MmcbE6s7eNkrABR9u37qCtJH5YcYL
set PORT=17890

set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

> "%STARTUP%\kronos_qmt_bridge.cmd" (
  echo @echo off
  echo cd /d "%~dp0"
  echo python "%~dp0qmt_bridge.py" --userdata "%USERDATA%" --account %ACCOUNT% --token %TOKEN% --port %PORT% --host 0.0.0.0
)

echo Created: %STARTUP%\kronos_qmt_bridge.cmd
echo Bridge will auto-start on next login.
echo (To test now, run start_bridge.bat)
pause
