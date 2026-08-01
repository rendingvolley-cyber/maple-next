@echo off
setlocal

rem Official Windows launcher entrypoint for Maple Next (Issue #31 Lane A).
rem Resolves its own directory via %~dp0 so it works from any working
rem directory and with spaces in the path, then hands off to the PowerShell
rem implementation. Exit code of the app/smoke run is propagated verbatim.
rem
rem Operator CLI (GNU-style, parsed one token at a time -- never as a single
rem stored %*  string):
rem   scripts\start_maple_next.cmd
rem   scripts\start_maple_next.cmd --smoke
rem   scripts\start_maple_next.cmd --runtime-root "D:\Maple Runtime"
rem   scripts\start_maple_next.cmd --smoke --runtime-root "D:\Maple Runtime"
rem   scripts\start_maple_next.cmd --runtime-root "D:\Maple Runtime" --smoke
rem
rem Argument errors jump to unparenthesized labels below before exiting:
rem `exit /b` executed *inside* a parenthesized if-block does not reliably
rem propagate a non-zero errorlevel out of this script, so no exit /b call
rem in this file is ever nested inside "( ... )".

set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%start_maple_next.ps1"

set "SMOKE_FLAG="
set "RUNTIME_ROOT_VALUE="
set "RUNTIME_ROOT_SET=0"

:parse_args
if "%~1"=="" goto args_done

if /i "%~1"=="--smoke" (
    set "SMOKE_FLAG=1"
    shift
    goto parse_args
)

if /i "%~1"=="--runtime-root" goto handle_runtime_root_flag

goto unknown_argument

:handle_runtime_root_flag
if "%RUNTIME_ROOT_SET%"=="1" goto duplicate_runtime_root
if "%~2"=="" goto missing_runtime_root_value

set "RUNTIME_ROOT_VALUE=%~2"
set "RUNTIME_ROOT_SET=1"
shift
shift
goto parse_args

:args_done

if "%RUNTIME_ROOT_SET%"=="1" (
    if defined SMOKE_FLAG (
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Smoke -RuntimeRoot "%RUNTIME_ROOT_VALUE%"
    ) else (
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RuntimeRoot "%RUNTIME_ROOT_VALUE%"
    )
) else (
    if defined SMOKE_FLAG (
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Smoke
    ) else (
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
    )
)
set "EXITCODE=%ERRORLEVEL%"

endlocal & exit /b %EXITCODE%

:duplicate_runtime_root
echo Maple Next launcher: --runtime-root was specified more than once.>&2
endlocal & exit /b 1

:missing_runtime_root_value
echo Maple Next launcher: --runtime-root requires a value.>&2
endlocal & exit /b 1

:unknown_argument
echo Maple Next launcher: unknown argument "%~1">&2
endlocal & exit /b 1
