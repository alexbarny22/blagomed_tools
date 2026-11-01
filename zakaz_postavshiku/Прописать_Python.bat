@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Ищу Python и прописываю его в PATH...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_python_path.ps1"
echo.
echo Готово. Закройте ВСЕ открытые чёрные окна и программы-боты,
echo после этого Python будет находиться везде.
pause
