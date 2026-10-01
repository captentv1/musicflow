@echo off
cd /d "%~dp0"
start "" http://127.0.0.1:5090
if exist ".venv\Scripts\python.exe" (".venv\Scripts\python.exe" app.py) else (python app.py)
pause
