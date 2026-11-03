@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ===== Установка репрайсера Ozon =====
echo.
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo [!] Python не найден.
  echo     Установите Python с сайта python.org - кнопка Downloads.
  echo     В установщике ОБЯЗАТЕЛЬНО поставьте галочку "Add python.exe to PATH".
  echo     Потом снова запустите этот файл.
  pause
  exit /b 1
)
echo Создаю окружение...
%PY% -m venv venv
if errorlevel 1 ( echo [!] Не удалось создать окружение. & pause & exit /b 1 )
echo Устанавливаю библиотеки (нужен интернет)...
venv\Scripts\python.exe -m pip install --upgrade pip -q
venv\Scripts\python.exe -m pip install -r requirements.txt -q
if errorlevel 1 ( echo [!] Ошибка установки библиотек. Проверьте интернет и запустите снова. & pause & exit /b 1 )
echo Создаю ярлык на рабочем столе...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Repricer Ozon.lnk'); $s.TargetPath='%~dp0venv\Scripts\pythonw.exe'; $s.Arguments='\"%~dp0app.py\"'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%SystemRoot%\System32\shell32.dll,43'; $s.Save()"
echo.
echo ===== Готово! =====
echo На рабочем столе появился ярлык "Repricer Ozon" - запускайте его.
echo.
pause
