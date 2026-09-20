@echo off
rem Launch the wowadmin control panel on Windows.
rem
rem Arguments are passed straight through to app.py:
rem
rem     run.bat                              find the config automatically
rem     run.bat --config C:\wow\realm.toml   drive a particular realm
rem     run.bat --no-browser                 do not open a tab
rem     run.bat --print-config               show the config, fully resolved
rem
rem Closing this window stops the panel. A server with console = true has its
rem input on an ordinary pipe, so on Windows it shuts down when the panel does
rem — see "Windows" in README.md. Servers without a console keep running.

setlocal
cd /d "%~dp0"

rem Only single-line ifs below: a set inside a parenthesised block is expanded
rem when the block is parsed, not when it runs, which is the classic way for a
rem batch file to quietly use the wrong value.
set "PYTHON="

rem py.exe is the Windows launcher and selects the newest interpreter.
where py >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
if defined PYTHON goto :run

where python >nul 2>nul
if not errorlevel 1 set "PYTHON=python"
if defined PYTHON goto :run

echo wowadmin needs Python 3.11 or newer, and no Python was found on PATH.
echo Install it from https://www.python.org/downloads/ and tick
echo "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:run
%PYTHON% app.py %*
set "RC=%errorlevel%"

rem Launched from Explorer rather than a terminal, the window would vanish on
rem an error before anything could be read. Hold it open when something failed.
if not "%RC%"=="0" echo.
if not "%RC%"=="0" echo wowadmin exited with code %RC%.
if not "%RC%"=="0" pause
exit /b %RC%
