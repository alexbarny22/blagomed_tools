@echo off
chcp 65001 >nul
cd /d "%~dp0"
python update.py
python direct_report.py
if errorlevel 1 pause
start "" "report\index.html"
