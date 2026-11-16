@echo off
set "BTA_PYTHON="
if defined BOUNTYTRACKER_PYTHON (
    set BTA_PYTHON="%BOUNTYTRACKER_PYTHON%"
    goto check
)
set "BTA_PYTHON=py -3"
call :probe
if not errorlevel 1 exit /b 0
set "BTA_PYTHON=python"
:check
call :probe
if not errorlevel 1 exit /b 0
echo Python 3.11 or later with Tkinter is required. Check PATH or set BOUNTYTRACKER_PYTHON to python.exe. 1>&2
exit /b 1

:probe
%BTA_PYTHON% -c "import sys, tkinter; sys.exit(sys.version_info[:2] < (3, 11))" >nul 2>&1
exit /b %errorlevel%
