@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist venv\Scripts\pythonw.exe (
  echo Сначала запустите 1_INSTALL.bat
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Repricer Ozon.lnk'); $s.TargetPath='%~dp0venv\Scripts\pythonw.exe'; $s.Arguments='\"%~dp0app.py\" --no-browser'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo Готово: репрайсер будет запускаться сам при включении компьютера (без открытия браузера).
pause
