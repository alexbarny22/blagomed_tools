# Находит установленный Python и прописывает его в PATH пользователя (навсегда)
param([switch]$Quiet)
$ErrorActionPreference = 'SilentlyContinue'
function Say($t) { if (-not $Quiet) { Write-Host $t } }

function Test-Py($exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    if ($exe -match 'WindowsApps') { return $false }
    & $exe -c "import sys; sys.exit(0 if sys.version_info[0]==3 else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

$cands = @()
# реестр (официальный установщик пишет сюда путь)
foreach ($root in 'HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore') {
    Get-ChildItem $root | ForEach-Object {
        $p = (Get-ItemProperty "$($_.PSPath)\InstallPath").'(default)'
        if ($p) { $cands += (Join-Path $p 'python.exe') }
        $e = (Get-ItemProperty "$($_.PSPath)\InstallPath").ExecutablePath
        if ($e) { $cands += $e }
    }
}
# запускатор py
$fromPy = & py -3 -c "import sys;print(sys.executable)" 2>$null
if ($fromPy) { $cands += $fromPy.Trim() }
# обычные папки
foreach ($d in "$env:LOCALAPPDATA\Programs\Python", $env:ProgramFiles, ${env:ProgramFiles(x86)}, 'C:\') {
    Get-ChildItem $d -Directory -Filter 'Python3*' | ForEach-Object { $cands += (Join-Path $_.FullName 'python.exe') }
}
# Anaconda / Miniconda
foreach ($d in "$env:USERPROFILE\anaconda3", "$env:USERPROFILE\miniconda3", 'C:\ProgramData\anaconda3', 'C:\ProgramData\miniconda3') {
    $cands += (Join-Path $d 'python.exe')
}

# берём самую свежую версию
$best = $null; $bestVer = [version]'0.0'
foreach ($c in ($cands | Select-Object -Unique)) {
    if (Test-Py $c) {
        $v = & $c -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
        try { $vv = [version]$v.Trim() } catch { continue }
        if ($vv -gt $bestVer) { $best = $c; $bestVer = $vv }
    }
}
if (-not $best) {
    Say 'Python не найден на этом компьютере.'
    exit 1
}
$dir = Split-Path $best -Parent
$scripts = Join-Path $dir 'Scripts'
Say "Найден Python $bestVer : $best"

# PATH пользователя читаем «как есть», чтобы не испортить %переменные%
$key = Get-Item 'HKCU:\Environment'
$old = $key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
$parts = @($old -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ne $dir.TrimEnd('\') -and $_.TrimEnd('\') -ne $scripts.TrimEnd('\') })
$newPath = (@($dir, $scripts) + $parts) -join ';'
if ($newPath -ne $old) {
    Set-ItemProperty -Path 'HKCU:\Environment' -Name 'Path' -Value $newPath -Type ExpandString
    # сообщаем Windows, что переменные изменились
    [Environment]::SetEnvironmentVariable('PYPATHFIX', '1', 'User')
    [Environment]::SetEnvironmentVariable('PYPATHFIX', $null, 'User')
    Say "Прописано в PATH: $dir"
    Say "               и: $scripts"
} else {
    Say 'PATH уже был настроен правильно.'
}

# убираем заглушки Microsoft Store, которые перехватывают команду python
$wa = "$env:LOCALAPPDATA\Microsoft\WindowsApps"
foreach ($f in 'python.exe', 'python3.exe') {
    if (Test-Path "$wa\$f") { Remove-Item "$wa\$f" -Force; Say "Отключена заглушка Microsoft Store: $f" }
}
Write-Output $best
exit 0
