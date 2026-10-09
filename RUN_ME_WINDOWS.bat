@echo off
setlocal
cd /d "%~dp0"
echo ===== AI Website Tester - one-click demo =====
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/ ^(tick "Add Python to PATH"^),
  echo then double-click this file again.
  pause
  exit /b 1
)
if not exist .venv (
  echo Creating a private Python environment...
  python -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
echo Installing packages ^(first run only, takes 1-3 minutes^)...
python -m pip install -q -r requirements.txt || goto :fail
python -m playwright install chromium || goto :fail
echo.
echo OPTIONAL: paste an Anthropic API key to get AI explanations in the report.
echo Just press Enter to skip - the tester still finds the bugs, with short built-in hints.
set /p ANTHROPIC_API_KEY=API key (Enter to skip): 
set AIFLAG=
if "%ANTHROPIC_API_KEY%"=="" set AIFLAG=--no-ai
echo.
echo ===== STEP 1: testing the version with planted bugs =====
python tester.py site\harborline-homes-buggy.html --out reports\before --headed %AIFLAG% || goto :fail
echo.
echo ===== STEP 2: testing the fixed version and comparing with step 1 =====
python tester.py site\harborline-homes-fixed.html --out reports\after --compare reports\before\results.json --headed %AIFLAG% || goto :fail
start "" reports\before\report.html
start "" reports\after\report.html
echo.
echo Done. Two reports were opened in your browser.
pause
exit /b 0
:fail
echo.
echo Something went wrong. Please read the message above.
pause
exit /b 1
