<#
  agent-sync/apply.ps1
  Установка НЕСЕКРЕТНЫХ правил, скиллов, конфигурации Cline и готовых программ
  из общей папки Яндекс Диска «АГЕНТЫ» на этот компьютер.

  Источник (синхронизирует Яндекс Диск):
      %USERPROFILE%\YandexDisk\АГЕНТЫ

  Куда раскладывается:
      АГЕНТЫ\rules\*.md                -> %USERPROFILE%\.cline\rules\
      АГЕНТЫ\skills\<имя>\SKILL.md     -> %USERPROFILE%\.cline\<имя>\
      АГЕНТЫ\cline-config\*.json       -> %USERPROFILE%\.cline\data\settings\ (белый список)
      АГЕНТЫ\apps\*                    -> C:\blagomed_tools\apps\

  Секреты не копируются никогда (чёрный список ниже): providers.json, secrets.json,
  *.env, *.key, *.pem, config.py/ini, файлы с token/secret/password в имени.

  Запуск вручную:
      powershell -NoProfile -ExecutionPolicy Bypass -File apply.ps1
  Автоматически — задача планировщика Agents2-Sync (см. install-task.ps1).
#>
[CmdletBinding()]
param(
  [string]$SourceRoot = (Join-Path $env:USERPROFILE 'YandexDisk\АГЕНТЫ'),
  [string]$ClineHome  = (Join-Path $env:USERPROFILE '.cline'),
  [string]$AppsDir    = 'C:\blagomed_tools\apps',
  [string]$StateFile  = 'C:\blagomed_tools\agent-sync\state\apply-last.json',
  [string]$LogFile    = (Join-Path $env:USERPROFILE '.cline\data\logs\agents2-sync.log')
)

$ErrorActionPreference = 'Continue'
$result = [ordered]@{
  started_at   = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
  source       = $SourceRoot
  source_found = $false
  rules        = 0
  skills       = @()
  configs      = @()
  apps         = 0
  skipped      = @()
  errors       = @()
}

$DenyNames   = @('secrets.json', 'providers.json', 'config.py', 'config.ini',
                 'config.json', 'credentials.json', 'hosts.yml')
$DenyExt     = @('.env', '.key', '.pem', '.pfx', '.p12', '.kdbx')
$AllowConfig = @('global-settings.json', 'cline_mcp_settings.json')

function Write-Log {
  param([string]$Message, [string]$Level = 'INFO')
  $line = "[{0}] {1,-5} {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
  try {
    $dir = Split-Path -Parent $LogFile
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    if ((Test-Path -LiteralPath $LogFile) -and ((Get-Item -LiteralPath $LogFile).Length -gt 1MB)) {
      Move-Item -LiteralPath $LogFile -Destination ($LogFile + '.1') -Force
    }
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
  } catch { }
}

function Test-Denied {
  param([System.IO.FileInfo]$File)
  if ($DenyNames -contains $File.Name.ToLowerInvariant()) { return $true }
  if ($DenyExt -contains $File.Extension.ToLowerInvariant()) { return $true }
  if ($File.Name -match '(?i)token|secret|password') { return $true }
  return $false
}

function Copy-IfChanged {
  param([string]$From, [string]$To)
  $dir = Split-Path -Parent $To
  if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  if (Test-Path -LiteralPath $To) {
    $h1 = (Get-FileHash -LiteralPath $From -Algorithm SHA256).Hash
    $h2 = (Get-FileHash -LiteralPath $To   -Algorithm SHA256).Hash
    if ($h1 -eq $h2) { return $false }
  }
  Copy-Item -LiteralPath $From -Destination $To -Force -ErrorAction Stop
  return $true
}

Write-Log '=== apply start ==='

if (-not (Test-Path -LiteralPath $SourceRoot)) {
  Write-Log "Папка АГЕНТЫ не найдена: $SourceRoot — синхронизировать нечего" 'WARN'
  $result.skipped += 'source-missing'
} else {
  $result.source_found = $true

  # --- 1. Правила ---
  $rulesSrc = Join-Path $SourceRoot 'rules'
  if (Test-Path -LiteralPath $rulesSrc) {
    foreach ($f in Get-ChildItem -LiteralPath $rulesSrc -File -Filter '*.md' -ErrorAction SilentlyContinue) {
      if (Test-Denied $f) { $result.skipped += $f.Name; continue }
      try {
        if (Copy-IfChanged $f.FullName (Join-Path $ClineHome ('rules\' + $f.Name))) {
          $result.rules++
          Write-Log "rule  + $($f.Name)"
        }
      } catch { $result.errors += "rule $($f.Name)"; Write-Log "rule FAIL $($f.Name): $($_.Exception.Message)" 'ERROR' }
    }
  }

  # --- 2. Скиллы (одна папка = один скилл, внутри SKILL.md) ---
  $skillsSrc = Join-Path $SourceRoot 'skills'
  if (Test-Path -LiteralPath $skillsSrc) {
    foreach ($d in Get-ChildItem -LiteralPath $skillsSrc -Directory -ErrorAction SilentlyContinue) {
      if (-not (Test-Path -LiteralPath (Join-Path $d.FullName 'SKILL.md'))) {
        $result.skipped += ('skill без SKILL.md: ' + $d.Name)
        Write-Log "skill SKIP $($d.Name): нет SKILL.md" 'WARN'
        continue
      }
      $dest = Join-Path $ClineHome $d.Name
      $changed = 0
      foreach ($f in Get-ChildItem -LiteralPath $d.FullName -Recurse -File -ErrorAction SilentlyContinue) {
        if (Test-Denied $f) { $result.skipped += $f.Name; continue }
        $rel = $f.FullName.Substring($d.FullName.Length).TrimStart('\')
        try {
          if (Copy-IfChanged $f.FullName (Join-Path $dest $rel)) { $changed++ }
        } catch { $result.errors += "skill $($d.Name)/$rel" }
      }
      if ($changed -gt 0) { Write-Log "skill + $($d.Name) ($changed файл(ов))" }
      $result.skills += ("{0} ({1})" -f $d.Name, $changed)
    }
  }

  # --- 3. Конфигурация Cline (только белый список, с бэкапом) ---
  $cfgSrc = Join-Path $SourceRoot 'cline-config'
  if (Test-Path -LiteralPath $cfgSrc) {
    foreach ($f in Get-ChildItem -LiteralPath $cfgSrc -File -Filter '*.json' -ErrorAction SilentlyContinue) {
      if ($AllowConfig -notcontains $f.Name.ToLowerInvariant()) {
        $result.skipped += ('config не в белом списке: ' + $f.Name)
        continue
      }
      $dest = Join-Path (Join-Path $ClineHome 'data\settings') $f.Name
      try {
        if (Test-Path -LiteralPath $dest) {
          $h1 = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
          $h2 = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
          if ($h1 -eq $h2) { continue }
          Copy-Item -LiteralPath $dest -Destination ($dest + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')) -Force
        }
        Copy-IfChanged $f.FullName $dest | Out-Null
        $result.configs += $f.Name
        Write-Log "config + $($f.Name) (прежний сохранён в .bak-*)"
      } catch { $result.errors += "config $($f.Name)"; Write-Log "config FAIL $($f.Name): $($_.Exception.Message)" 'ERROR' }
    }
  }

  # --- 4. Готовые программы ---
  $appsSrc = Join-Path $SourceRoot 'apps'
  if (Test-Path -LiteralPath $appsSrc) {
    if (-not (Test-Path -LiteralPath $AppsDir)) { New-Item -ItemType Directory -Force -Path $AppsDir | Out-Null }
    foreach ($f in Get-ChildItem -LiteralPath $appsSrc -File -ErrorAction SilentlyContinue) {
      if (Test-Denied $f) { $result.skipped += $f.Name; continue }
      try {
        if (Copy-IfChanged $f.FullName (Join-Path $AppsDir $f.Name)) {
          $result.apps++
          Write-Log "app   + $($f.Name)"
        }
      } catch { $result.errors += "app $($f.Name)"; Write-Log "app FAIL $($f.Name): $($_.Exception.Message)" 'ERROR' }
    }
  }
}

$result.finished_at = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$stateDir = Split-Path -Parent $StateFile
if ($stateDir -and -not (Test-Path -LiteralPath $stateDir)) { New-Item -ItemType Directory -Force -Path $stateDir | Out-Null }
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StateFile -Encoding UTF8
Write-Log ("=== apply done: rules={0} skills={1} configs={2} apps={3} errors={4} ===" -f $result.rules, $result.skills.Count, $result.configs.Count, $result.apps, $result.errors.Count)
if ($result.errors.Count -gt 0) { exit 1 }
exit 0

