@echo off
rem Run anything in this project with THIS project's interpreter, from cmd.
rem
rem     run deployed
rem     run smoke
rem     run landed
rem     run gates
rem     run emails daily --dry-run
rem
rem ## Why this exists
rem
rem `run` is a bash script. Every command handed over for weeks was written
rem `./run ...`, which does nothing in cmd.exe - the shell this project is
rem actually driven from. The commands were correct and unrunnable, and the
rem person typing them assumed the fault was theirs.
rem
rem So the two dispatchers sit side by side and tests\test_run_dispatch.py
rem requires them to offer the same commands. A Windows entry point silently
rem missing `smoke` would be the same defect one layer down.
rem
rem Written with labels rather than parenthesised one-liners on purpose: the
rem first draft used `( ... & exit /b !errorlevel! )`, which needs delayed
rem expansion it did not enable, and it hung instead of dispatching.

setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" goto makevenv
:ready

set "CMD=%~1"
if "%CMD%"=="" goto usage
shift

rem Everything after the command, preserved as typed.
set "ARGS="
:collect
if "%~1"=="" goto dispatch
set "ARGS=%ARGS% %1"
shift
goto collect

:dispatch
if /i "%CMD%"=="emails"   goto emails
if /i "%CMD%"=="deployed" goto deployed
if /i "%CMD%"=="smoke"    goto smoke
if /i "%CMD%"=="landed"   goto landed
if /i "%CMD%"=="test"     goto test
if /i "%CMD%"=="gates"    goto gates
if /i "%CMD%"=="python"   goto python
if /i "%CMD%"=="pip"      goto pip
rem Anything else runs through the project interpreter too, so
rem `run scripts\measure_onboarding.py` works without a special case.
"%PY%" "%CMD%"%ARGS%
exit /b %errorlevel%

:emails
"%PY%" scripts\send_emails.py%ARGS%
exit /b %errorlevel%

:deployed
"%PY%" scripts\deployed.py%ARGS%
exit /b %errorlevel%

:smoke
"%PY%" scripts\smoke.py%ARGS%
exit /b %errorlevel%

:landed
"%PY%" scripts\landed.py%ARGS%
exit /b %errorlevel%

:test
"%PY%" -m pytest%ARGS%
exit /b %errorlevel%

:gates
bash scripts/gates.sh%ARGS%
exit /b %errorlevel%

:python
"%PY%"%ARGS%
exit /b %errorlevel%

:pip
"%PY%" -m pip%ARGS%
exit /b %errorlevel%

:makevenv
echo No venv here yet - creating one.
python -m venv .venv
"%PY%" -m pip install --quiet --upgrade pip
rem EDITABLE, so src/ edits take effect without reinstalling.
"%PY%" -m pip install --quiet -e ".[dev]"
echo Ready.
goto ready

:usage
echo Usage: run {emails^|deployed^|smoke^|landed^|test^|gates^|python^|pip} ...
echo.
echo   run deployed                   is production running origin/main? run it first
echo   run smoke                      sign in to production and read every surface
echo   run landed [commit ...]        is the work committed, pushed, on main? run it last
echo   run gates                      everything that must pass
echo   run emails daily --dry-run     what today's brief would say
exit /b 2
