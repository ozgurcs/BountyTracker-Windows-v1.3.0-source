@echo off
setlocal
cd /d "%~dp0" || exit /b 1
call "%~dp0python-windows.bat" || exit /b 1
%BTA_PYTHON% "%~dp0app\main.py"
exit /b %errorlevel%
