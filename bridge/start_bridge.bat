@echo off
REM ============================================================
REM  Kronos QMT Bridge - one-click START (cloud Windows server)
REM
REM  1. Edit the 3 lines below (USERDATA / ACCOUNT / TOKEN)
REM  2. Double-click this file.
REM
REM  USERDATA: the "userdata_mini" folder of your QMT install,
REM            e.g. D:\QMT\userdata_mini  (right-click QMT
REM            shortcut -> Properties to find install path)
REM  ACCOUNT  : your fund account number
REM  TOKEN    : keep the pre-generated one, or make your own
REM ============================================================

set USERDATA=D:\QMT\userdata_mini
set ACCOUNT=40735103
set TOKEN=Vk9MmcbE6s7eNkrABR9u37qCtJH5YcYL
set PORT=17890

echo [Kronos QMT Bridge] starting on port %PORT% ...
python "%~dp0qmt_bridge.py" --userdata "%USERDATA%" --account %ACCOUNT% --token %TOKEN% --port %PORT%
pause
