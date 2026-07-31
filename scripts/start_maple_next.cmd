@echo off
setlocal

rem Official Windows launcher entrypoint for Maple Next (Issue #31 Lane A).
rem Resolves its own directory via %~dp0 so it works from any working
rem directory and with spaces in the path, then hands off to the PowerShell
rem implementation. Exit code of the app/smoke run is propagated verbatim.

set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%start_maple_next.ps1"

rem Accept the conventional "--smoke" spelling as an alias for -Smoke so
rem `scripts\start_maple_next.cmd --smoke` works alongside `-Smoke`.
set "ARGS=%*"
if /i "%ARGS%"=="--smoke" set "ARGS=-Smoke"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %ARGS%
set "EXITCODE=%ERRORLEVEL%"

endlocal & exit /b %EXITCODE%
