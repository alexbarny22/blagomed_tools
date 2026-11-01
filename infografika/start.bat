@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Infografika Blagomed
python update.py
python deps.py
if errorlevel 1 (pause & exit /b)
python deps.py config
if errorlevel 1 (pause & exit /b)
python app.py
pause
