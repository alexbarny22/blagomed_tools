<#
  agent-sync/install-task.ps1
  Регистрирует в планировщике задачу, которая каждые N минут раскладывает
  правила/скиллы/конфиг Cline из папки АГЕНТЫ и проверяет ответ сопряжения.

  Права администратора не нужны (задача пользователя, /RL LIMITED).
  Использование:
      powershell -NoProfile -ExecutionPolicy Bypass -File install-task.ps1
      powershell ... -File install-task.ps1 -EveryMinutes 30
#>
[CmdletBinding()]
param(
  [string]$TaskName = 'Agents2-Sync',
  [int]$EveryMinutes = 15,
  [string]$Root = 'C:\blagomed_tools\agent-sync'
)

$ErrorActionPreference = 'Continue'
$runner = Join-Path $Root 'run-sync.ps1'
if (-not (Test-Path -LiteralPath $runner)) { Write-Host "Нет $runner"; exit 1 }

$tr = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ' + $runner

# 1) Периодический запуск
$out = & schtasks.exe /Create /TN $TaskName /TR $tr /SC MINUTE /MO $EveryMinutes /RL LIMITED /F 2>&1
Write-Host ($out -join ' ')

# 2) Дополнительно при входе пользователя
$out2 = & schtasks.exe /Create /TN ($TaskName + '-Logon') /TR $tr /SC ONLOGON /RL LIMITED /F 2>&1
Write-Host ($out2 -join ' ')

# Немедленный первый прогон
& schtasks.exe /Run /TN $TaskName 2>&1 | Out-Null

$state = & schtasks.exe /Query /TN $TaskName /FO LIST 2>&1
Write-Host ($state | Select-Object -First 6 | Out-String)
