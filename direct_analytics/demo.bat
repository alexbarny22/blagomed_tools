@echo off
chcp 65001 >nul
cd /d "%~dp0"
python direct_report.py --demo
if errorlevel 1 pause
start "" "report\index.html"
