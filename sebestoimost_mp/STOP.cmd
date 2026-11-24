@echo off
chcp 866 >nul
echo Останавливаю сервер Себестоимость МП...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'run\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output ('Остановлен процесс PID ' + $_.ProcessId) }"
echo Готово.
pause
