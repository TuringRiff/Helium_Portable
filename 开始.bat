@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "APP_DIR=%~dp0Helium"
set "HELIUM_EXE=%APP_DIR%\chrome.exe"
set "HELIUM_SHORTCUT=%~dp0Helium.lnk"
set "HELIUM_ARGUMENTS=--custom-update-server-url=https://updates.invalid/ --no-default-browser-check"
set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if exist "%HELIUM_EXE%" goto browser_found
echo ERROR: Helium\chrome.exe was not found.
goto failed

:browser_found
if exist "%POWERSHELL_EXE%" goto powershell_found
set "POWERSHELL_EXE=powershell.exe"

:powershell_found
"%POWERSHELL_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut($env:HELIUM_SHORTCUT); $l.TargetPath=$env:HELIUM_EXE; $l.Arguments=$env:HELIUM_ARGUMENTS; $l.WorkingDirectory=$env:APP_DIR; $l.IconLocation=($env:HELIUM_EXE + ',0'); $l.Save()"
if errorlevel 1 goto failed

if exist "%HELIUM_SHORTCUT%" goto success

:failed
echo Shortcut creation failed.
pause
exit /b 1

:success
echo Shortcut created successfully:
echo "%HELIUM_SHORTCUT%"
exit /b 0
