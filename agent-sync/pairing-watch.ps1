<#
  agent-sync/pairing-watch.ps1
  Обработчик одноразового сопряжения с VPS (CEZAR).

  Как это работает:
    1) Этот компьютер кладёт запрос в АГЕНТЫ\cezar\pairing\out\request-<ХОСТ>.json.
       В запросе только ПУБЛИЧНЫЙ ключ. Приватный ключ не покидает компьютер никогда.
    2) Машина с уже авторизованным административным каналом (домашний ПК)
       выполняет cezar-authorize.ps1: добавляет публичный ключ в
       /home/cezar/.ssh/authorized_keys и, ТОЛЬКО после успешной проверки,
       формирует ответ. Этот скрипт сам вызывает authorize для pending-запроса
       другой машины, если задан канал: $env:AGENTS_ADMIN_SSH = <ssh-алиас>.
    3) Ответ кладётся в АГЕНТЫ\cezar\pairing\in\response-<ХОСТ>.json:
         { "pairing_id": "...", "host": "SKLAD", "status": "approved", ... }
    4) Этот скрипт (задача планировщика) видит ответ, проверяет SSH-доступ,
       поднимает туннель и проверяет панель. Итог пишется в out\result-<ХОСТ>.json.

  Скрипт ничего не меняет на VPS и не читает приватные ключи.

  Использование:
      powershell -NoProfile -ExecutionPolicy Bypass -File pairing-watch.ps1
#>
[CmdletBinding()]
param(
  [string]$PairRoot = (Join-Path $env:USERPROFILE 'YandexDisk\АГЕНТЫ\cezar\pairing'),
  [string]$SshHost  = 'cezar-vps',
  [string]$LogFile  = (Join-Path $env:USERPROFILE '.cline\data\logs\agents2-sync.log'),
  [string]$AgentSync = 'C:\blagomed_tools\agent-sync'
)

$ErrorActionPreference = 'Continue'
$hostName = $env:COMPUTERNAME

function Write-Log {
  param([string]$Message, [string]$Level = 'INFO')
  $line = "[{0}] {1,-5} pair  {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
  try {
    $dir = Split-Path -Parent $LogFile
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
  } catch { }
}

$inDir  = Join-Path $PairRoot 'in'
$outDir = Join-Path $PairRoot 'out'
if (-not (Test-Path -LiteralPath $inDir))  { New-Item -ItemType Directory -Force -Path $inDir  | Out-Null }
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }

# 0. Обратное направление того же протокола: pending-запросы других машин.
#    Обрабатывает только машина, у которой явно задан авторизованный канал.
$adminHost = $env:AGENTS_ADMIN_SSH
$pending = Get-ChildItem -LiteralPath $outDir -File -Filter 'request-*.json' -ErrorAction SilentlyContinue
foreach ($rf in $pending) {
  $rq = $null
  try { $rq = Get-Content -LiteralPath $rf.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
  $reqHost = [string]$rq.machine.hostname
  if (-not $reqHost -or $reqHost -eq $hostName) { continue }
  $respFile = Join-Path $inDir ("response-{0}.json" -f $reqHost)
  if (Test-Path -LiteralPath $respFile) { continue }

  if (-not $adminHost) {
    Write-Log ("pending-запрос от {0}: канал не задан (AGENTS_ADMIN_SSH) — ответ не формируется" -f $reqHost) 'WARN'
    continue
  }

  $auth = Join-Path (Split-Path -Parent $PSCommandPath) 'cezar-authorize.ps1'
  if (-not (Test-Path -LiteralPath $auth)) { $auth = Join-Path $AgentSync 'cezar-authorize.ps1' }
  if (-not (Test-Path -LiteralPath $auth)) { Write-Log "нет cezar-authorize.ps1 ($auth)" 'ERROR'; continue }

  Write-Log ("pending-запрос от {0}: привязка ключа через канал '{1}'" -f $reqHost, $adminHost)
  $p = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru -Wait -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $auth,
    '-AdminHost', $adminHost, '-PairRoot', $PairRoot, '-RequesterHost', $reqHost)
  Write-Log ("cezar-authorize: exit={0}, ответ появился={1}" -f $p.ExitCode, (Test-Path -LiteralPath $respFile))
}

$responses = Get-ChildItem -LiteralPath $inDir -File -Filter '*.json' -ErrorAction SilentlyContinue
if (-not $responses) { exit 0 }

foreach ($f in $responses) {
  $r = $null
  try { $r = Get-Content -LiteralPath $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { Write-Log "битый ответ $($f.Name)" 'WARN'; continue }

  $forMe = ($r.host -eq $hostName) -or ($r.host -eq '*')
  if (-not $forMe) { continue }

  $status = [string]$r.status
  $id = [string]$r.pairing_id
  if (-not $id) { $id = [System.IO.Path]::GetFileNameWithoutExtension($f.Name) }

  $doneFile = Join-Path $outDir ("result-{0}-{1}.json" -f $hostName, $id)
  if (Test-Path -LiteralPath $doneFile) { continue }   # уже обработан

  Write-Log ("получен ответ сопряжения id=$id status=$status")

  $out = [ordered]@{
    pairing_id  = $id
    host        = $hostName
    status      = $status
    processed_at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    ssh_ok      = $false
    ssh_output  = ''
    panel_ok    = $false
  }

  if ($status -ne 'approved') {
    $out.ssh_output = 'сопряжение не одобрено — доступ не настраивается'
    $out | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $doneFile -Encoding UTF8
    Write-Log "сопряжение id=$id отклонено" 'WARN'
    continue
  }

  # Проверка SSH строго в неинтерактивном режиме: ничего не вводим и не подбираем
  $tmp = Join-Path $env:TEMP ("pair-ssh-{0}.txt" -f $id)
  Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c',
      ("ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new {0} ""echo PAIR_OK; hostname"" > ""{1}"" 2>&1" -f $SshHost, $tmp)
    ) -WindowStyle Hidden -Wait | Out-Null
  $sso = if (Test-Path -LiteralPath $tmp) { (Get-Content -LiteralPath $tmp -Raw) } else { '' }
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue

  $out.ssh_output = $sso.Trim()
  $out.ssh_ok = ($sso -match 'PAIR_OK')
  Write-Log ("ssh после сопряжения: ok=" + $out.ssh_ok)

  if ($out.ssh_ok) {
    $check = Join-Path $AgentSync 'cezar-check.ps1'
    if (Test-Path -LiteralPath $check) {
      $p = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru -Wait -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $check, '-StartTunnel')
      $statusFile = Join-Path (Join-Path $env:USERPROFILE 'YandexDisk\АГЕНТЫ\cezar\status') ("{0}-cezar.json" -f $hostName)
      if (Test-Path -LiteralPath $statusFile) {
        try {
          $st = Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
          $out.panel_ok = [bool]$st.panel_ok
        } catch { }
      }
      Write-Log ("проверка панели: exit=" + $p.ExitCode + " panel_ok=" + $out.panel_ok)
    }
  }

  $out | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $doneFile -Encoding UTF8
}
exit 0
