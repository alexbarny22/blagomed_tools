@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Prihod po foto
python update.py
python deps.py
if errorlevel 1 (pause & exit /b)
python deps.py config
if errorlevel 1 (pause & exit /b)
:loop
python prihod_bot.py
echo Bot stopped, restart in 15 seconds. Close this window to stop.
timeout /t 15 >nul
goto loop
