@echo off
cd /d "%~dp0"
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --name MusicFlow --add-data "index.html;." --collect-data imageio_ffmpeg --collect-binaries imageio_ffmpeg --collect-data selenium --collect-submodules yt_dlp --hidden-import truststore app.py > build_exe.log 2>&1
if errorlevel 1 (echo EXE_KO>> build_exe.log) else (echo EXE_OK>> build_exe.log)
