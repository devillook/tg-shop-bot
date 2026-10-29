@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -U -r requirements.txt
echo.
pause
