@echo off
chcp 65001 >nul
cd /d "%~dp0"
title VK bot
python update.py
python deps.py
if errorlevel 1 (pause & exit /b)
:loop
python VK_bot.py --no-update
echo Bot stopped, restart in 15 seconds. Close this window to stop.
timeout /t 15 >nul
python update.py
goto loop
