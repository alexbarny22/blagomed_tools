# Публикация страницы бота через IIS на уже открытом порту 4001: http://<сервер>:4001/movebot
# IIS сам пересылает запросы боту (127.0.0.1:8090). Нужны модули IIS URL Rewrite и ARR — ставятся автоматически.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Port = 8090
$Dir  = "C:\inetpub\movebot"
function Step($t) { Write-Host ""; Write-Host "=== $t" -ForegroundColor Cyan }
function Fail($t) { Write-Host ""; Write-Host "ОШИБКА: $t" -ForegroundColor Red; Read-Host "Нажмите Enter"; exit 1 }

Import-Module WebAdministration

function Install-Msi($name, $url, $check) {
    if (Test-Path $check) { Write-Host "  $name уже установлен"; return }
    Write-Host "  скачиваю $name…"
    $f = "$env:TEMP\$name.msi"
    Invoke-WebRequest -Uri $url -OutFile $f -UseBasicParsing
    Start-Process msiexec.exe -ArgumentList "/i `"$f`" /quiet /norestart" -Wait
    if (-not (Test-Path $check)) { Fail "$name не установился" }
    Write-Host "  $name установлен"
}

Step "1. Модули IIS"
Install-Msi "URL Rewrite" "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi" "$env:SystemRoot\System32\inetsrv\rewrite.dll"
Install-Msi "Application Request Routing" "https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi" "$env:ProgramFiles\IIS\Application Request Routing\requestRouter.dll"
Set-WebConfigurationProperty -PSPath "MACHINE/WEBROOT/APPHOST" -Filter "system.webServer/proxy" -Name "enabled" -Value "True"
Write-Host "  пересылка запросов (proxy) включена"

Step "2. Сайт IIS с портом 4001"
$site = Get-Website | Where-Object { $_.Bindings.Collection | Where-Object { $_.bindingInformation -like "*:4001:*" } } | Select-Object -First 1
if (-not $site) { Fail "не найден сайт IIS на порту 4001" }
Write-Host "  сайт: $($site.Name)"

Step "3. Адрес /movebot"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
@"
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="movebot" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:$Port/{R:1}" />
        </rule>
      </rules>
    </rewrite>
  </system.webServer>
</configuration>
"@ | Set-Content -Path "$Dir\web.config" -Encoding UTF8
if (Get-WebVirtualDirectory -Site $site.Name -Name "movebot" -ErrorAction SilentlyContinue) {
    Remove-WebVirtualDirectory -Site $site.Name -Application "/" -Name "movebot"
}
if (Get-WebApplication -Site $site.Name -Name "movebot" -ErrorAction SilentlyContinue) {
    Remove-WebApplication -Site $site.Name -Name "movebot"
}
New-WebVirtualDirectory -Site $site.Name -Name "movebot" -PhysicalPath $Dir | Out-Null
Write-Host "  готово"

Step "4. Перезапуск бота (подтянет свежую страницу)"
Stop-ScheduledTask -TaskName "move_bot_web" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*web.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName "move_bot_web"
Start-Sleep -Seconds 25

$ip = ($site.Bindings.Collection | Where-Object { $_.bindingInformation -like "*:4001:*" } | Select-Object -First 1).bindingInformation.Split(":")[0]
if (-not $ip -or $ip -eq "*") { $ip = "91.199.149.224" }
try {
    Invoke-WebRequest -Uri "http://$ip`:4001/movebot/" -UseBasicParsing -TimeoutSec 15 | Out-Null
    Write-Host "  страница отвечает через IIS" -ForegroundColor Green
} catch {
    if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) {
        Write-Host "  страница отвечает через IIS (просит пароль — так и должно быть)" -ForegroundColor Green
    } else { Write-Host "  проверка не прошла: $($_.Exception.Message)" -ForegroundColor Yellow }
}

Step "Готово"
Write-Host "Откройте на любом компьютере:  http://$ip`:4001/movebot/" -ForegroundColor Green
Read-Host "Нажмите Enter, чтобы закрыть"
