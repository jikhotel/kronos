param(
    [string]$ProjectRoot = "",
    [string]$ZipPath = "",
    [string]$Tag = "v1.7",
    [switch]$RestartOnly,
    [switch]$ExposePort80,
    [switch]$InstallAutostart
)

# Kronos cloud deployment helper for Windows Server.
#
# 一键完整部署（推荐，已在 123.207.186.241 / Windows Server 2022 实测）：
#   powershell -ExecutionPolicy Bypass -File .\deploy_cloud.ps1 `
#       -ProjectRoot C:\kronos -ExposePort80 -InstallAutostart
#
# 注意：云端到 github.com / codeload.github.com 直连会超时，
#       脚本会自动回退到 ghproxy.net 镜像；也可用 -ZipPath 传入本地包。
# 通过 WinRM 调用时，单条 PowerShell 命令长度上限约 3-4KB，
#       不要用内联 base64 传大文件（会报 "The command line is too long"）。

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Find-ProjectRoot {
    param([string]$Hint)
    if ($Hint -and (Test-Path (Join-Path $Hint "webui\app.py"))) { return $Hint }
    $roots = New-Object System.Collections.ArrayList
    [void]$roots.Add((Get-Location).Path)
    foreach ($base in @("C:\", "D:\", "C:\Users\Administrator", "C:\Users\Admin", "C:\inetpub", "C:\www", "D:\www")) {
        if (-not (Test-Path $base)) { continue }
        Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            [void]$roots.Add($_.FullName)
        }
    }
    foreach ($r in $roots) {
        if (Test-Path (Join-Path $r "webui\app.py")) { return $r }
    }
    throw "Cannot locate Kronos project (webui\app.py not found). Pass -ProjectRoot."
}

$root = Find-ProjectRoot -Hint $ProjectRoot
Write-Host "Project root : $root"

if (-not $RestartOnly) {
    if (-not $ZipPath) {
        $ZipPath = Join-Path $env:TEMP "kronos-$Tag.zip"
        $archive = "https://github.com/jikhotel/kronos/archive/refs/tags/$Tag.zip"
        # 云端直连 GitHub 常超时，镜像站实测可用
        $urls = @($archive, "https://ghproxy.net/$archive")
        $ok = $false
        foreach ($url in $urls) {
            try {
                Write-Host "Downloading : $url"
                $ProgressPreference = 'SilentlyContinue'
                Invoke-WebRequest -Uri $url -OutFile $ZipPath -UseBasicParsing -TimeoutSec 300
                if ((Get-Item $ZipPath).Length -gt 100000) { $ok = $true; break }
            } catch {
                Write-Warning "download failed: $($_.Exception.Message.Split([char]10)[0])"
            }
        }
        if (-not $ok) { throw "Cannot download $Tag archive (GitHub direct and ghproxy both failed)" }
    }
    if (-not (Test-Path $ZipPath)) { throw "Zip not found: $ZipPath" }
    Write-Host "Using zip   : $ZipPath"

    $stage = Join-Path $env:TEMP ("kronos_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Expand-Archive -Path $ZipPath -DestinationPath $stage -Force
    $inner = Get-ChildItem -Path $stage -Directory | Select-Object -First 1
    if (-not $inner) { throw "Empty archive" }
    Write-Host "Staged      : $($inner.FullName)"

    # 只更新受版本管理的源码；运行时文件（users.db / .secret_key / 预测历史 /
    # 模型缓存 / 既有 *.db）一律保留，避免覆盖线上账号与数据。
    $n = 0
    Get-ChildItem -LiteralPath $inner.FullName -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($inner.FullName.Length + 1)
        if ($rel -like 'webui\prediction_results\*') { return }
        if ($rel -like '*.db' -and $rel -notlike 'quant\artifacts\*') { return }
        # 不要用发布包里的旧版助手覆盖正在运行的脚本自身
        if ($rel -eq 'deploy_cloud.ps1') { return }
        $dest = Join-Path $root $rel
        $dir = Split-Path $dest -Parent
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
        $n++
    }
    Write-Host "Copied files: $n"

    # 用 v1.7 快照替换量化库（441 次回测产物：9 策略 / 8 共享 / 19 回测记录）
    $srcDb = Join-Path $root "quant\artifacts\quant_1.7.db"
    $dstDb = Join-Path $root "quant\quant.db"
    if (Test-Path $srcDb) {
        if (Test-Path $dstDb) {
            $bak = "$dstDb.bak_" + (Get-Date -Format "yyyyMMdd_HHmmss")
            Copy-Item $dstDb $bak -Force
            Write-Host "DB backup   : $bak"
        }
        Copy-Item $srcDb $dstDb -Force
        # 残留 WAL/SHM 会把新库覆盖回旧数据
        Remove-Item "$dstDb-wal" -Force -ErrorAction SilentlyContinue
        Remove-Item "$dstDb-shm" -Force -ErrorAction SilentlyContinue
        Write-Host "DB updated  : $dstDb"
    } else {
        Write-Warning "quant_1.7.db not found in package; existing quant.db kept"
    }

    $ver = Get-Content (Join-Path $root "VERSION") -Raw
    Write-Host "VERSION     : $($ver.Trim())"
}

# 重启 Flask，沿用原有解释器与参数；默认 python.exe / app.py + webui 工作目录
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -match 'app\.py' })
$pyExe = "python.exe"
$pyArgs = "app.py"
if ($procs.Count -gt 0) {
    $p = $procs[0]
    if ($p.ExecutablePath) { $pyExe = $p.ExecutablePath }
    $idx = $p.CommandLine.IndexOf("app.py")
    if ($idx -ge 0) { $pyArgs = $p.CommandLine.Substring($idx) }
}
$wd = Join-Path $root "webui"
$outLog = Join-Path $wd "service_stdout.log"
$errLog = Join-Path $wd "service_stderr.log"

if ($InstallAutostart) {
    # 计划任务由任务计划程序托管，不受 WinRM 会话回收影响，开机自动拉起
    $taskName = "Kronos WebUI"
    $a = New-ScheduledTaskAction -Execute $pyExe -Argument $pyArgs -WorkingDirectory $wd
    $t = New-ScheduledTaskTrigger -AtStartup
    $p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $st = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $taskName -Action $a -Trigger $t -Principal $p -Settings $st -Force | Out-Null
    Write-Host "Autostart   : scheduled task '$taskName' (SYSTEM / AtStartup)"
    $running = @(Get-ScheduledTask -TaskName $taskName | Where-Object { $_.State -eq 'Running' })
    if ($running.Count -gt 0) {
        Write-Host "Autostart   : task already running, skip direct start"
        $skipStart = $true
    }
    if (-not $skipStart -and (Get-ScheduledTask -TaskName $taskName).State -eq 'Ready') {
        # 无现存服务时直接交给计划任务托管，避免双实例抢 7070
        Start-ScheduledTask -TaskName $taskName
        Write-Host "Started via : scheduled task"
        $skipStart = $true
    }
}

if (-not $skipStart) {
    $procs | ForEach-Object {
        Write-Host "Stopping PID: $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($procs.Count -gt 0) { Start-Sleep -Seconds 2 }
    Write-Host "Starting    : $pyExe $pyArgs (cwd $wd)"
    Start-Process -FilePath $pyExe -ArgumentList $pyArgs -WorkingDirectory $wd `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
}
Start-Sleep -Seconds 10

if ($ExposePort80) {
    # 公网安全组通常只放行 80，用端口转发把 80 映射到本机 7070
    Set-Service iphlpsvc -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service iphlpsvc -ErrorAction SilentlyContinue
    & netsh interface portproxy delete v4tov4 listenport=80 listenaddress=0.0.0.0 | Out-Null
    & netsh interface portproxy add v4tov4 listenport=80 listenaddress=0.0.0.0 connectport=7070 connectaddress=127.0.0.1
    New-NetFirewallRule -DisplayName "Kronos Web 80" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow -ErrorAction SilentlyContinue | Out-Null
    New-NetFirewallRule -DisplayName "Kronos Web 7070" -Direction Inbound -Protocol TCP -LocalPort 7070 -Action Allow -ErrorAction SilentlyContinue | Out-Null
    Write-Host "Port proxy  : 0.0.0.0:80 -> 127.0.0.1:7070"
}

foreach ($port in @(7070, 80)) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 8
        Write-Host "CHECK OK    : port $port -> HTTP $($r.StatusCode)"
    } catch {
        Write-Host "CHECK FAIL  : port $port -> $($_.Exception.Message.Split([char]10)[0])"
    }
}
Write-Host "Done. Logs: $outLog / $errLog"
