@echo off
setlocal
cd /d "%~dp0" || exit /b 1
call "%~dp0python-windows.bat" || exit /b 1
%BTA_PYTHON% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    %BTA_PYTHON% -m pip install PyInstaller
    if errorlevel 1 exit /b 1
)
%BTA_PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed --name BountyTracker --icon "app\bountytracker.ico" --add-data "app\bountytracker.ico:." "%~dp0app\main.py"
if errorlevel 1 exit /b 1
echo Built: "%~dp0dist\BountyTracker.exe"

