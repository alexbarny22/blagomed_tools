<#
  agent-sync/cezar-authorize.ps1
  Готовит и (при доступности) выполняет привязку складского ключа к пользователю
  cezar на VPS через УЖЕ авторизованный административный канал.

  Принципы:
    * никакой обход аутентификации: работает только BatchMode (без паролей/подсказок);
    * если авторизованного канала нет — скрипт останавливается и печатает блокер;
    * /root/.ssh, sudoers, sshd_config и firewall не изменяются;
    * добавляется ровно один публичный ключ (вся логика — в add-sklad-key.sh).

  Примеры:
    powershell -File cezar-authorize.ps1 -AdminHost home-access -DryRun
    powershell -File cezar-authorize.ps1 -AdminHost home-access
#>
[CmdletBinding()]
param(
  [string]$AdminHost,
  [string]$LocalScript = 'C:\blagomed_tools\agent-sync\cezar\add-sklad-key.sh',
  [string]$ClientAlias = 'cezar-vps',
  [string]$LogFile = (Join-Path $env:USERPROFILE '.cline\data\logs\agents2-sync.log'),
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Write-Log {
  param([string]$Message, [string]$Level = 'INFO')
  $line = "[{0}] {1,-5} auth  {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
  try {
    $dir = Split-Path -Parent $LogFile
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
  } catch { }
}

function Invoke-Ssh {
  param([string]$Target, [string]$Command, [int]$TimeoutSec = 25)
  # stderr нативной команды не должен превращаться в terminating error
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & ssh.exe -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $Target $Command 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  return @{ Code = $code; Out = (($out | ForEach-Object { "$_" }) -join "`n").Trim() }
}

if (-not (Test-Path -LiteralPath $LocalScript)) { throw "нет скрипта: $LocalScript" }

if (-not $AdminHost) {
  Write-Log 'не задан -AdminHost: нет авторизованного административного канала' 'ERROR'
  Write-Host 'БЛОКЕР: с этого компьютера нет авторизованного административного канала к VPS.'
  Write-Host 'Запустите с машины, у которой доступ уже есть:'
  Write-Host "  powershell -File $PSCommandPath -AdminHost <ssh-алиас>"
  Write-Host 'Либо выполните на VPS: sudo bash АГЕНТЫ/cezar/provision/add-sklad-key.sh'
  exit 1
}

# 1. Проверка, что канал действительно авторизован (без интерактива).
$probe = Invoke-Ssh -Target $AdminHost -Command 'echo ADMIN_OK'
if ($probe.Code -ne 0 -or $probe.Out -notmatch 'ADMIN_OK') {
  Write-Log ("канал '$AdminHost' не авторизован: " + $probe.Out) 'ERROR'
  Write-Host "БЛОКЕР: канал '$AdminHost' не авторизован ($($probe.Out))."
  Write-Host 'Обход аутентификации не выполняется. Нужен уже открытый канал.'
  exit 1
}
$who = (Invoke-Ssh -Target $AdminHost -Command 'id -un').Out
Write-Log "канал '$AdminHost' авторизован, логин: $who"

# 2. Доставка скрипта (только чтение локального файла, запись в /tmp канала).
$remotePath = '/tmp/add-sklad-key.sh'
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$scpOut = & scp.exe -o BatchMode=yes -o ConnectTimeout=10 $LocalScript "${AdminHost}:$remotePath" 2>&1
$scpCode = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($scpCode -ne 0) {
  Write-Log ("не удалось доставить скрипт: " + (($scpOut | ForEach-Object { "$_" }) -join ' ')) 'ERROR'
  throw "scp не выполнен: $((($scpOut | ForEach-Object { "$_" }) -join ' '))"
}
Write-Log "скрипт доставлен в $remotePath на $AdminHost"

# 3. Выбор способа запуска: root напрямую или sudo без пароля.
$cmd = if ($DryRun) { '--dry-run' } else { '' }
if ($who -eq 'root') {
  $run = "bash $remotePath $cmd"
} else {
  $sudo = Invoke-Ssh -Target $AdminHost -Command 'sudo -n true'
  if ($sudo.Code -ne 0) {
    Write-Log 'sudo без пароля недоступен — требуется ручной запуск' 'WARN'
    Write-Host 'Канал авторизован, скрипт доставлен, но sudo требует пароль.'
    Write-Host 'Выполните вручную (одна команда):'
    Write-Host "  ssh $AdminHost 'sudo bash $remotePath $cmd'"
    exit 3
  }
  $run = "sudo -n bash $remotePath $cmd"
}

$res = Invoke-Ssh -Target $AdminHost -Command $run -TimeoutSec 60
Write-Host $res.Out
Write-Log ("выполнение на $AdminHost (exit=$($res.Code)): " + $res.Out.Replace("`n", ' | ')) 
if ($res.Code -ne 0) {
  Write-Log 'скрипт привязки завершился с ошибкой' 'ERROR'
  exit 2
}

if ($DryRun) { Write-Host 'DRY-RUN завершён: изменения на VPS не вносились.'; exit 0 }

# 4. Проверка со стороны склада: вход под cezar и живой туннель-порт.
$check = Invoke-Ssh -Target $ClientAlias -Command 'echo CEZAR_OK'
if ($check.Code -eq 0 -and $check.Out -match 'CEZAR_OK') {
  Write-Log 'складской ключ принят VPS: ssh cezar-vps работает'
  Write-Host 'ГОТОВО: складской ключ принят. Ярлык «СЕЗАР» поднимет туннель сам.'
  exit 0
}

Write-Log ('ключ добавлен, но вход cezar-vps не удался: ' + $check.Out) 'ERROR'
Write-Host "Ключ добавлен, но вход '$ClientAlias' не удался: $($check.Out)"
exit 4
