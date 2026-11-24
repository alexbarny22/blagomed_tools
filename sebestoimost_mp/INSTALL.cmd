@echo off
chcp 866 >nul
setlocal
cd /d "%~dp0"
echo === Себестоимость МП: установка ===

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo [ОШИБКА] Python 3 не найден. Установите Python 3.11+ с python.org и повторите.
  pause
  exit /b 1
)

if not exist venv (
  echo Создаю виртуальное окружение venv...
  %PY% -m venv venv
  if errorlevel 1 (
    echo [ОШИБКА] Не удалось создать venv.
    pause
    exit /b 1
  )
) else (
  echo venv уже существует - пропускаю создание.
)

echo Ставлю зависимости...
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo [ОШИБКА] Не удалось установить зависимости.
  pause
  exit /b 1
)

if not exist config.ini (
  copy /y config.example.ini config.ini >nul
  echo Создан config.ini - заполните параметры подключения к 1С.
) else (
  echo config.ini уже существует - не перезаписываю.
)

if exist data\sebestoimost.db (
  echo База данных найдена - не перезаписываю.
) else (
  echo База данных будет создана автоматически при первом запуске.
)

echo === Установка завершена. Запуск: START.cmd ===
pause
