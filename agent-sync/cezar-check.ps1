<#
  agent-sync/cezar-check.ps1
  Проверка доступа к панели CEZAR с этого компьютера.

  Успех засчитывается ТОЛЬКО если одновременно:
    1) открыт локальный порт 4321 (SSH-туннель к ai-agent-vps),
    2) http://127.0.0.1:4321/api/v1/health отвечает HTTP 200.

  Результат пишется в АГЕНТЫ\cezar\status\<ХОСТ>-cezar.json (виден с других машин)
  и в лог %USERPROFILE%\.cline\data\logs\agents2-sync.log.

  Ключи и пароли не читаются и не копируются. Состояние VPS не меняется.

  Использование:
      powershell -NoProfile -ExecutionPolicy Bypass -File cezar-check.ps1
      powershell ... -File cezar-check.ps1 -StartTunnel   # поднять туннель, если ключ уже сопряжён
#>
[CmdletBinding()]
param(
  [int]$Port = 4321,
  [string]$SshHost = 'cezar-vps',
  [string]$PanelPath = '/p/agents-2/',
  [string]$StatusDir = (Join-Path $env:USERPROFILE 'YandexDisk\АГЕНТЫ\cezar\status'),
  [string]$LogFile = (Join-Path $env:USERPROFILE '.cline\data\logs\agents2-sync.log'),
  [switch]$StartTunnel
)

$ErrorActionPreference = 'Continue'
$hostName = $env:COMPUTERNAME

function Write-Log {
  param([string]$Message, [string]$Level = 'INFO')
  $line = "[{0}] {1,-5} cezar {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
  try {
    $dir = Split-Path -Parent $LogFile
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
  } catch { }
}

function Test-LocalPort {
  param([int]$P)
  $client = $null
  try {
    $client = New-Object Net.Sockets.TcpClient
    $iar = $client.BeginConnect('127.0.0.1', $P, $null, $null)
    return ($iar.AsyncWaitHandle.WaitOne(500) -and $client.Connected)
  } catch { return $false } finally { if ($client) { $client.Close() } }
}

$res = [ordered]@{
  checked_at   = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
  host         = $hostName
  user         = $env:USERNAME
  port         = $Port
  ssh_host     = $SshHost
  tunnel_up    = $false
  http_status  = $null
  health       = $null
  panel_path   = $PanelPath
  panel_status = $null
  panel_ok     = $false
  verdict      = 'tunnel-down'
  hint         = ''
}

if (-not (Test-LocalPort -P $Port) -and $StartTunnel) {
  # Туннель поднимаем тем же способом, что cezar-tunnel.ps1, но без открытия браузера
  $bind = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress
  if ($bind) {
    Write-Log "поднимаю туннель через $bind"
    Start-Process -FilePath 'ssh' -WindowStyle Hidden -ArgumentList @(
      '-b', $bind, '-N',
      '-o', 'ExitOnForwardFailure=yes',
      '-o', 'ServerAliveInterval=30',
      '-o', 'ServerAliveCountMax=3',
      '-o', 'BatchMode=yes',
      '-L', "$Port`:127.0.0.1:$Port",
      $SshHost
    ) | Out-Null
    for ($i = 0; $i -lt 15; $i++) {
      Start-Sleep -Milliseconds 700
      if (Test-LocalPort -P $Port) { break }
    }
  } else {
    $res.hint = 'нет LAN-адреса 192.168.* (Wi-Fi выключен?)'
  }
}

$res.tunnel_up = Test-LocalPort -P $Port
if ($res.tunnel_up) {
  try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/v1/health" -f $Port) -UseBasicParsing -TimeoutSec 15
    $res.http_status = [int]$r.StatusCode
    if ($r.StatusCode -eq 200) {
      $res.health = $r.Content
      $res.panel_ok = $true
      $res.verdict = 'ok'
      # Проверка самой панели (не только health)
      try {
        $p = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}{1}" -f $Port, $PanelPath) -UseBasicParsing -TimeoutSec 15
        $res.panel_status = [int]$p.StatusCode
      } catch {
        $res.panel_status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
      }
    } else {
      $res.verdict = 'panel-error'
    }
  } catch {
    $res.verdict = 'panel-unreachable'
    $res.hint = $_.Exception.Message
  }
} else {
  $res.verdict = 'tunnel-down'
  if (-not $res.hint) { $res.hint = 'порт 4321 закрыт: SSH-туннель не поднят (ключ не сопряжён или VPS недоступен)' }
}

if (-not (Test-Path -LiteralPath $StatusDir)) { New-Item -ItemType Directory -Force -Path $StatusDir | Out-Null }
$file = Join-Path $StatusDir ("{0}-cezar.json" -f $hostName)
$res | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $file -Encoding UTF8
Write-Log ("verdict={0} tunnel={1} http={2}" -f $res.verdict, $res.tunnel_up, $res.http_status)

Write-Host ("CEZAR [{0}]: {1}" -f $hostName, $res.verdict)
if ($res.panel_ok) { Write-Host "Панель отвечает: http://127.0.0.1:$Port"; exit 0 }
if ($res.hint) { Write-Host ("Причина: " + $res.hint) }
exit 1
