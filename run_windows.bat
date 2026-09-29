@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Sanal ortam olusturuluyor...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3.10+ bulunamadi. https://www.python.org/downloads/ adresinden kurun.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Bagimliliklar kurulamadi.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m livedub %*
