<#
  agent-sync/cezar-open.ps1
  Ярлык «СЕЗАР»: поднимает SSH-туннель к панели CEZAR (если его ещё нет)
  и открывает http://127.0.0.1:4321/p/agents-2/ в браузере по умолчанию.

  Ключ и хост-ключ берутся из профиля ~/.ssh/config "cezar-vps".
  Пароли не запрашиваются (BatchMode), приватный ключ никуда не уходит.
#>
[CmdletBinding()]
param(
  [string]$SshAlias = 'cezar-vps',
  [int]$Port = 4321,
  [string]$PanelPath = '/p/agents-2/',
  [string]$LogFile = (Join-Path $env:USERPROFILE '.cline\data\logs\agents2-sync.log'),
  [switch]$NoPrompt
)

$ErrorActionPreference = 'Continue'

function Write-Log {
  param([string]$Message, [string]$Level = 'INFO')
  $line = "[{0}] {1,-5} open  {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
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
    return ($iar.AsyncWaitHandle.WaitOne(600) -and $client.Connected)
  } catch { return $false } finally { if ($client) { $client.Close() } }
}

function Show-Message {
  param([string]$Text, [string]$Title = 'СЕЗАР', [int]$Seconds = 20)
  if ($NoPrompt) { Write-Host $Text; return }
  try { (New-Object -ComObject WScript.Shell).Popup($Text, $Seconds, $Title, 48) | Out-Null }
  catch { Write-Host $Text }
}

$url = "http://127.0.0.1:$Port$PanelPath"

if (-not (Test-LocalPort -P $Port)) {
  $bind = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress
  $sshArgs = @('-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
               '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
               '-L', "127.0.0.1:${Port}:127.0.0.1:${Port}")
  if ($bind) { $sshArgs = @('-b', $bind) + $sshArgs }
  $sshArgs += $SshAlias
  Write-Log ("поднимаю туннель: ssh " + ($sshArgs -join ' '))
  try { Start-Process -FilePath 'ssh' -ArgumentList $sshArgs -WindowStyle Hidden | Out-Null }
  catch { Write-Log ("ssh не запустился: " + $_.Exception.Message) 'ERROR' }

  for ($i = 0; $i -lt 25; $i++) {
    Start-Sleep -Milliseconds 800
    if (Test-LocalPort -P $Port) { break }
  }
}

if (-not (Test-LocalPort -P $Port)) {
  Write-Log 'туннель не поднялся: порт 4321 закрыт (ключ не привязан или VPS недоступен)' 'ERROR'
  Show-Message ("Панель CEZAR недоступна.`n`nПорт 4321 не открылся: SSH-ключ склада ещё не добавлен в /home/cezar/.ssh/authorized_keys на VPS.`n`nЧто нужно: один раз выполнить на VPS  sudo bash add-sklad-key.sh  (папка АГЕНТЫ\cezar\provision).`n`nПодробности: %USERPROFILE%\.cline\data\logs\agents2-sync.log")
  exit 1
}

$code = $null
try {
  $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 20
  $code = [int]$r.StatusCode
} catch {
  if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } else { $code = 0 }
}

Write-Log ("туннель ок, {0} -> {1}" -f $url, $code)

if ($code -ge 200 -and $code -lt 400) {
  Start-Process $url
  exit 0
}

Show-Message ("Туннель поднят, но панель ответила кодом $code.`nАдрес: $url`n`nПроверьте, что сервис CEZAR на VPS запущен.")
exit 1
