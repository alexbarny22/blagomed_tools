@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo Тестовый запуск: ответы только показываются, на WB ничего не отправляется.
echo.

set PY=

rem 1) Лаунчер py (ставится вместе с обычным Python)
py -3 -c "import sys" >nul 2>nul && set PY=py -3

rem 2) python из PATH, но не заглушка из Магазина Microsoft (WindowsApps)
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
        if not defined PY set PY="%%i"
    )
)

rem 3) Обычные папки установки Python
if not defined PY (
    for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*" "C:\Program Files\Python3*" "C:\Python3*") do (
        if exist "%%d\python.exe" set PY="%%d\python.exe"
    )
)

if not defined PY (
    echo Не нашёл Python на этом компьютере.
    echo Откройте bots_manager.log в папке менеджера: во второй строке после запуска
    echo написано «интерпретатор: ...» — пришлите этот путь, я пропишу его сюда.
    echo.
    pause
    exit /b
)

echo Python: %PY%
echo.
%PY% wb_reviews_bot.py --test

echo.
pause
