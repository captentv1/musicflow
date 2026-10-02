@echo off
cd /d "%~dp0"
set JAVA_HOME=C:\dev\jdk17
call .\gradlew.bat assembleRelease --no-daemon > build.log 2>&1
if errorlevel 1 (echo BUILD_KO>> build.log) else (echo BUILD_OK>> build.log)
