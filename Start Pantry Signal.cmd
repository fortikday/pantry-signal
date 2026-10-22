@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    py -3 "%~dp0app.py"
    goto finished
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    python "%~dp0app.py"
    goto finished
  )
)
echo Install Python 3.11 or newer from python.org, then open this launcher again.
echo Choose Add Python to PATH during installation.
pause
exit /b 1
:finished
if errorlevel 1 pause
endlocal
