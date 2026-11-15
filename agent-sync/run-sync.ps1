<#
  agent-sync/run-sync.ps1
  Одна точка входа для планировщика: раскладывает файлы из АГЕНТЫ
  и обрабатывает ответ сопряжения CEZAR, если он появился.
#>
[CmdletBinding()]
param()

$root = Split-Path -Parent $PSCommandPath
$ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

foreach ($script in @('apply.ps1', 'pairing-watch.ps1', 'cezar-check.ps1')) {
  $path = Join-Path $root $script
  if (Test-Path -LiteralPath $path) {
    & $ps -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $path
  }
}
exit 0
