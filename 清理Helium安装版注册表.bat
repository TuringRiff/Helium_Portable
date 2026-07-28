@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "CLEANUP_SCRIPT=%~dp0scripts\cleanup_helium_registry.ps1"
set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "SCRIPT_ARGS="
if /I "%~1"=="--dry-run" set "SCRIPT_ARGS=-DryRun"

if exist "%CLEANUP_SCRIPT%" goto script_found
echo ERROR: Cleanup script was not found:
echo "%CLEANUP_SCRIPT%"
goto failed_before_run

:script_found
if exist "%POWERSHELL_EXE%" goto powershell_found
set "POWERSHELL_EXE=powershell.exe"

:powershell_found
echo Helium installed-version registry cleanup is starting.
echo Browser files and user data will not be deleted.
echo Helium Portable will not be registered by this script.
echo.

"%POWERSHELL_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CLEANUP_SCRIPT%" %SCRIPT_ARGS%
set "RESULT=%ERRORLEVEL%"

echo.
if "%RESULT%"=="0" goto success
echo ERROR: Cleanup did not finish successfully. Exit code: %RESULT%
goto finished

:success
if /I "%~1"=="--dry-run" goto dry_run_success
echo Cleanup finished.
echo You can now set the default browser from inside Helium Portable.
goto finished

:dry_run_success
echo Dry run finished. No registry entries were changed.
goto finished

:failed_before_run
set "RESULT=1"

:finished
echo.
pause
exit /b %RESULT%
