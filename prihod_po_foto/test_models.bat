@echo off
chcp 65001 >nul
cd /d "%~dp0"
python deps.py test
if errorlevel 1 (pause & exit /b)
python test_models.py
pause
