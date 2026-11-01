@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Prihod po foto
python update.py
python deps.py
if errorlevel 1 (pause & exit /b)
if not exist config.py (
  copy config.example.py config.py >nul
  echo Создан config.py - заполните его в Блокноте и запустите start.bat снова.
  notepad config.py
  pause
  exit /b
)
:loop
python prihod_bot.py
echo Бот остановился, перезапуск через 15 секунд... (закройте окно, чтобы остановить)
timeout /t 15 >nul
goto loop
