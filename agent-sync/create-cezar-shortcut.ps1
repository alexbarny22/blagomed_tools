<#
  agent-sync/create-cezar-shortcut.ps1
  Создаёт на рабочем столе ярлык «СЕЗАР», который поднимает туннель
  и открывает панель CEZAR без домашнего компьютера.
#>
[CmdletBinding()]
param(
  [string]$Root = 'C:\blagomed_tools\agent-sync',
  [string]$Name = 'СЕЗАР'
)

$ErrorActionPreference = 'Stop'
$target = Join-Path $Root 'cezar-open.ps1'
if (-not (Test-Path -LiteralPath $target)) { throw "Нет $target" }

$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop ($Name + '.lnk')
$ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $ps
$sc.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$target`""
$sc.WorkingDirectory = $Root
$sc.IconLocation = "$env:WINDIR\System32\shell32.dll,13"
$sc.Description = 'CEZAR: туннель к VPS и открытие панели /p/agents-2/'
$sc.WindowStyle = 7
$sc.Save()

Write-Host "Ярлык создан: $lnk"
