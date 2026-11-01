@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ==== Имя скрипта бота (для другого бота поменяйте только эту строку) ====
set "SCRIPT=zakaz_postavshiku.py"

set "PY="

rem --- 1. путь, найденный в прошлый раз
if exist "python_path.txt" set /p PY=<"python_path.txt"
if defined PY if not exist "!PY!" set "PY="

rem --- 2. стандартный запускатор py
if not defined PY call :try_cmd "py -3"

rem --- 3. python из PATH (кроме заглушки Microsoft Store)
if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
    if not defined PY call :check "%%i"
  )
)

rem --- 4. прописываем Python в PATH и берём найденный путь
if not defined PY (
  echo Python не прописан в PATH - исправляю...
  for /f "delims=" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_python_path.ps1" -Quiet') do call :check "%%i"
)
if not defined PY call :scan

rem --- 5. не нашли - ставим Python сами
if not defined PY (
  echo Python не найден. Устанавливаю Python 3.12 - подождите несколько минут...
  where winget >nul 2>nul && winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  call :scan
)
if not defined PY (
  echo Скачиваю установщик Python с python.org...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile \"$env:TEMP\py_setup.exe\""
  if exist "%TEMP%\py_setup.exe" "%TEMP%\py_setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_pip=1
  call :scan
)
if not defined PY (
  echo.
  echo НЕ УДАЛОСЬ найти или установить Python.
  echo Установите его вручную с https://www.python.org/downloads/
  echo и при установке поставьте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)

> "python_path.txt" echo !PY!
echo Python: !PY!

rem --- библиотеки (ставятся один раз)
"!PY!" -c "import requests, openpyxl, reportlab" 2>nul || (
  echo Устанавливаю библиотеки...
  "!PY!" -m ensurepip --upgrade >nul 2>nul
  "!PY!" -m pip install --disable-pip-version-check -r requirements.txt
)

rem --- запуск бота; окно с ошибкой не закроется
"!PY!" "%SCRIPT%"
if errorlevel 1 (
  echo.
  echo Бот завершился с ошибкой - текст выше. Пришлите его.
  pause
)
exit /b

rem ============ подпрограммы ============
:try_cmd
for /f "delims=" %%i in ('%~1 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY call :check "%%i"
exit /b

:check
rem принимаем только настоящий python.exe 3.x с tkinter
if not exist "%~1" exit /b
echo %~1 | findstr /i "WindowsApps" >nul && exit /b
"%~1" -c "import sys, tkinter; sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>nul && set "PY=%~1"
exit /b

:scan
for /d %%d in ("%LocalAppData%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "%ProgramFiles(x86)%\Python3*" "C:\Python3*") do (
  if exist "%%~d\python.exe" call :check "%%~d\python.exe"
)
if not defined PY call :try_cmd "py -3"
exit /b
