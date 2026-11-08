@echo off
cd /d "%~dp0.."
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --name MusicFlow --distpath dist --workpath build --specpath build --paths core --paths desktop --add-data "%CD%\web\index.html;." --collect-data imageio_ffmpeg --collect-binaries imageio_ffmpeg --collect-data selenium --collect-submodules yt_dlp --collect-data yt_dlp_ejs --hidden-import truststore desktop\app.py > build_exe.log 2>&1
if errorlevel 1 (echo EXE_KO>> build_exe.log) else (echo EXE_OK>> build_exe.log)
