@echo off
rem Runs print-to-thermal on Windows from cmd.exe or PowerShell: the Python
rem package in ..\thermal\, standard library only (Python 3.9 or newer).
setlocal
set "ROOT=%~dp0.."
set "BOOT=import sys; sys.path.insert(0, sys.argv.pop(1)); from thermal.cli import main; sys.exit(main())"
if defined THERMAL_PYTHON (
  "%THERMAL_PYTHON%" -B -c "%BOOT%" "%ROOT%" %*
  exit /b %ERRORLEVEL%
)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 -B -c "%BOOT%" "%ROOT%" %*
  exit /b %ERRORLEVEL%
)
python -B -c "%BOOT%" "%ROOT%" %*
exit /b %ERRORLEVEL%
