@echo off
cd /d "%~dp0"
start "" http://127.0.0.1:5090
python app.py
pause
