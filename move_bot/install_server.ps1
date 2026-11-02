# Установка серверной версии бота передачи (МГ) на сервер 1С.
# Запускается из install_server.bat (сам попросит права администратора).
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Repo   = "alexbarny22/blagomed_tools"
$Folder = "move_bot"
$Target = "C:\blagomed_tools\move_bot"
$Port   = 8090
$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path

function Step($t) { Write-Host ""; Write-Host "=== $t" -ForegroundColor Cyan }
function Fail($t) { Write-Host ""; Write-Host "ОШИБКА: $t" -ForegroundColor Red; Read-Host "Нажмите Enter"; exit 1 }

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-RepoFile($name, $dest) {
    $urls = @(
        "https://api.github.com/repos/$Repo/contents/$Folder/$name`?ref=main",
        "https://raw.githubusercontent.com/$Repo/main/$Folder/$name",
        "https://cdn.jsdelivr.net/gh/$Repo@main/$Folder/$name")
    foreach ($u in $urls) {
        try {
            Invoke-WebRequest -Uri $u -OutFile $dest -UseBasicParsing -TimeoutSec 30 `
                -Headers @{ "Accept" = "application/vnd.github.raw"; "User-Agent" = "blagomed-installer" }
            return $true
        } catch { Write-Host "  $u — не ответил" }
    }
    return $false
}

Step "1. Папка бота: $Target"
New-Item -ItemType Directory -Force -Path $Target | Out-Null
if (Test-Path "$Here\config.py") {
    if (Test-Path "$Target\config.py") {
        Write-Host "  config.py уже есть на сервере — оставляю его"
    } else {
        Copy-Item "$Here\config.py" "$Target\config.py"
        Write-Host "  config.py скопирован"
    }
} elseif (-not (Test-Path "$Target\config.py")) {
    Fail "рядом с установщиком нет config.py"
}

Step "2. Python"
$py = $null
foreach ($c in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python313\python.exe",
                 "C:\Program Files\Python311\python.exe")) { if (Test-Path $c) { $py = $c; break } }
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $py = $cmd.Source }
}
if (-not $py) {
    Write-Host "  Python не найден — скачиваю и устанавливаю (несколько минут)…"
    $inst = "$env:TEMP\python-3.12.7-amd64.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $inst -UseBasicParsing
    Start-Process -FilePath $inst -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
    $py = "C:\Program Files\Python312\python.exe"
    if (-not (Test-Path $py)) { Fail "Python не установился" }
}
Write-Host "  Python: $py"
Set-Content -Path "$Target\python_path.txt" -Value $py -Encoding ASCII

Step "3. Библиотеки"
& $py -m pip install --upgrade --quiet pip 2>$null
& $py -m pip install --quiet requests flask waitress
if ($LASTEXITCODE -ne 0) { Fail "не удалось установить библиотеки (pip)" }

Step "4. Файлы бота с GitHub"
if (-not (Get-RepoFile "update.py" "$Target\update.py")) { Fail "GitHub недоступен" }
Set-Content -Path "$Target\version.txt" -Value "0" -Encoding ASCII
Push-Location $Target
& $py update.py
Pop-Location
if (-not (Test-Path "$Target\web.py")) { Fail "файлы бота не скачались" }

Step "5. Брандмауэр: порт $Port"
netsh advfirewall firewall delete rule name="move_bot web" | Out-Null
netsh advfirewall firewall add rule name="move_bot web" dir=in action=allow protocol=TCP localport=$Port | Out-Null
Write-Host "  порт открыт"

Step "6. Автозапуск (задача Windows «move_bot_web»)"
schtasks /End /TN "move_bot_web" 2>$null | Out-Null
schtasks /Delete /TN "move_bot_web" /F 2>$null | Out-Null
schtasks /Create /TN "move_bot_web" /TR "`"$Target\server_run.bat`"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F | Out-Null
schtasks /Run /TN "move_bot_web" | Out-Null
Write-Host "  бот запущен и будет стартовать вместе с сервером"

Step "Готово"
Write-Host "Откройте в браузере на любом компьютере:  http://91.199.149.224:$Port" -ForegroundColor Green
Write-Host "Первый запуск загружает номенклатуру и цены из 1С — несколько минут."
Read-Host "Нажмите Enter, чтобы закрыть"
