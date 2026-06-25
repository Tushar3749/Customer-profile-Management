@echo off
cd /d "%~dp0"
echo Customer Call Log Profile Tool start hocche...
if not exist venv (
    echo Virtual environment banano hocche...
    py -m venv venv
    if errorlevel 1 (
        echo ERROR: venv bananao jacche na. "py --version" check koro.
        pause
        exit /b 1
    )
)
call venv\Scripts\activate.bat
echo Dependencies install hocche...
venv\Scripts\pip.exe install -r requirements.txt --quiet
if not exist .env (
    echo .env file pawa jaini!
    pause
    exit /b 1
)
echo Server cholche: http://127.0.0.1:8010
echo Bondho korte Ctrl+C chapo
venv\Scripts\python.exe -m uvicorn app:app --reload --port 8010
