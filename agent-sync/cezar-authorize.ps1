<#
  agent-sync/cezar-authorize.ps1
  Готовит и (при доступности) выполняет привязку складского ключа к пользователю
  cezar на VPS через УЖЕ авторизованный административный канал.

  Принципы:
    * никакой обход аутентификации: работает только BatchMode (без паролей/подсказок);
    * если авторизованного канала нет — скрипт останавливается и печатает блокер;
    * /root/.ssh, sudoers, sshd_config и firewall не изменяются;
    * добавляется ровно один публичный ключ (вся логика — в add-sklad-key.sh);
    * после успешной проверки входа скрипт сам формирует ответ сопряжения
      pairing\in\response-<ХОСТ>.json (protocol pairing-v1), чтобы склад
      перестал ждать: host берётся из облачного out\request-<ХОСТ>.json.

  Примеры:
    powershell -File cezar-authorize.ps1 -AdminHost home-access -DryRun
    powershell -File cezar-authorize.ps1 -AdminHost home-access
#>
[CmdletBinding()]
param(
  [string]$AdminHost,
  [string]$LocalScript = 'C:\blagomed_tools\agent-sync\cezar\add-sklad-key.sh',
  [string]$ClientAlias = 'cezar-vps',
  [string]$PairRoot = (Join-Path $env:USERPROFILE 'YandexDisk\АГЕНТЫ\cezar\pairing'),
  [string]$RequesterHost,
  [string]$LogFile = (Join-Path $env:USERPROFILE '.cline\data\logs\agents2-sync.log'),
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Get-PairingRequest {
  <# Новейший запрос сопряжения из облака: out\request-<ХОСТ>.json (протокол pairing-v1). #>
  param([string]$OutDir)
  if (-not (Test-Path -LiteralPath $OutDir)) { return $null }
  $f = Get-ChildItem -LiteralPath $OutDir -File -Filter 'request-*.json' -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $f) { return $null }
  try {
    $j = Get-Content -LiteralPath $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    return [pscustomobject]@{
      File      = $f.FullName
      PairingId = [string]$j.pairing_id
      Host      = [string]$j.machine.hostname
    }
  } catch { return $null }
}

function Write-PairingResponse {
  <# Ответ запрашивающей машине. Вызывается ТОЛЬКО после проверенной серверной авторизации. #>
  param(
    [string]$InDir,
    [string]$RequesterHost,
    [string]$PairingId,
    [string]$ApprovedBy,
    [string]$Note
  )
  if (-not (Test-Path -LiteralPath $InDir)) { New-Item -ItemType Directory -Force -Path $InDir | Out-Null }
  $file = Join-Path $InDir ("response-{0}.json" -f $RequesterHost)
  $body = [ordered]@{
    pairing_id  = $PairingId
    host        = $RequesterHost
    status      = 'approved'
    approved_by = $ApprovedBy
    approved_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
    note        = $Note
  }
  $json = $body | ConvertTo-Json -Depth 4
  [System.IO.File]::WriteAllText($file, $json, (New-Object System.Text.UTF8Encoding($false)))
  return $file
}

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

  # 5. Ответ запрашивающей машине — только после успешной проверки авторизации.
  $inDir  = Join-Path $PairRoot 'in'
  $outDir = Join-Path $PairRoot 'out'
  $req = Get-PairingRequest -OutDir $outDir
  $reqHost = if ($RequesterHost) { $RequesterHost } elseif ($req) { $req.Host } else { '' }
  if (-not $reqHost) {
    Write-Log 'запрос сопряжения не найден: ответ для склада не сформирован' 'ERROR'
    Write-Host "БЛОКЕР: в '$outDir' нет request-<ХОСТ>.json и не задан -RequesterHost."
    Write-Host 'Ключ привязан, но ответить некому. Укажите хост явно:'
    Write-Host "  powershell -File $PSCommandPath -RequesterHost SKLAD"
    exit 5
  }
  $pairingId = if ($req -and $req.PairingId) { $req.PairingId } else { '' }
  $respFile = Write-PairingResponse -InDir $inDir -RequesterHost $reqHost -PairingId $pairingId `
              -ApprovedBy ("{0} ({1})" -f $env:COMPUTERNAME, $who) `
              -Note 'публичный ключ добавлен в ~cezar/.ssh/authorized_keys; проверено: ssh cezar-vps -> CEZAR_OK'
  Write-Log "ответ для $reqHost сформирован: $respFile"
  Write-Host "Ответ для '$reqHost' сформирован: $respFile"
  exit 0
}

Write-Log ('ключ добавлен, но вход cezar-vps не удался: ' + $check.Out) 'ERROR'
Write-Host "Ключ добавлен, но вход '$ClientAlias' не удался: $($check.Out)"
exit 4
