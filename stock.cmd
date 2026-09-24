@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
set "COMMAND=%~1"
if "%COMMAND%"=="" set "COMMAND=help"

if not "%~3"=="" goto :unsupported
set "JSONARG="
if not "%~2"=="" (
    if /I "%~2"=="--json" (
        set "JSONARG=--json"
    ) else if /I "%~2"=="-Json" (
        set "JSONARG=--json"
    ) else (
        goto :unsupported
    )
)

if /I "%COMMAND%"=="help" goto :help
if /I "%COMMAND%"=="doctor" goto :helper
if /I "%COMMAND%"=="status" goto :helper
if /I "%COMMAND%"=="ui-command" goto :helper
if /I "%COMMAND%"=="ui" goto :ui
goto :unsupported

:help
if defined JSONARG goto :unsupported
echo Stock operator v2
echo   .\stock.cmd help        : this help; no Python needed
echo   .\stock.cmd doctor      : environment inventory; no auto-install/repair
echo   .\stock.cmd status      : bounded saved evidence; NOT live health
echo   .\stock.cmd ui-command  : print the existing GUI command; DO NOT launch it
echo   .\stock.cmd ui          : launch the existing Streamlit dashboard in foreground
echo   Add --json to doctor/status/ui-command only.
echo   This .cmd entrypoint does not depend on PowerShell execution policy.
echo Korean walkthrough: START_HERE.md
echo Collector start/stop/canary and backtest execution are NOT connected.
exit /b 0

:runtime
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Project .venv\Scripts\python.exe is missing. Read START_HERE.md.
    echo No environment was installed/changed. No .venv32 or PATH fallback.
    exit /b 2
)
exit /b 0

:helper
call :runtime
if errorlevel 1 exit /b %errorlevel%
set "ENTRYPOINT=%ROOT%scripts\stock_operator.py"
if not exist "%ENTRYPOINT%" (
    echo scripts\stock_operator.py is missing. No command was run.
    exit /b 2
)
if defined JSONARG (
    "%PYTHON%" -I -B "%ENTRYPOINT%" "%COMMAND%" --json
) else (
    "%PYTHON%" -I -B "%ENTRYPOINT%" "%COMMAND%"
)
exit /b %errorlevel%

:ui
if defined JSONARG goto :unsupported
call :runtime
if errorlevel 1 exit /b %errorlevel%
set "APP=%ROOT%dashboard\app.py"
if not exist "%APP%" (
    echo dashboard\app.py is missing. No process was launched.
    exit /b 2
)
echo Starting the existing Stock Streamlit UI in the foreground.
echo Close it with Ctrl+C in this terminal. Closing only the browser tab does not stop the server.
pushd "%ROOT%"
"%PYTHON%" -B -m streamlit run "%APP%" --server.address 127.0.0.1
set "EXITCODE=%errorlevel%"
popd
exit /b %EXITCODE%

:unsupported
echo Unsupported command/arguments. Run: .\stock.cmd help
exit /b 2
