param(
    [string]$ProjectRoot = "",
    [string]$ZipPath = "",
    [string]$Tag = "v1.10.2",
    [switch]$RestartOnly,
    [switch]$ExposePort80,
    [switch]$InstallAutostart,
    [switch]$InitQuantDbIfMissing
)

# Kronos cloud deployment helper for Windows Server.
#
# 一键完整部署（推荐，已在 123.207.186.241 / Windows Server 2022 实测）：
#   powershell -ExecutionPolicy Bypass -File .\deploy_cloud.ps1 `
#       -ProjectRoot C:\kronos -ExposePort80 -InstallAutostart
#
# 注意：云端优先 codeload.github.com，回退 GitHub archive 与 ghproxy.net 镜像；
#       每个候选都校验 zip 中央目录，截断包不会被部署；也可用 -ZipPath 传入本地包。
# 通过 WinRM 调用时，单条 PowerShell 命令长度上限约 3-4KB，
#       不要用内联 base64 传大文件（会报 "The command line is too long"）。
#
# 数据安全（v1.10.1 起）：
#   - 线上 users.db / .secret_key / quant.db / screener_data.json / 预测历史 / 缓存
#     一律保留，绝不覆盖（选股实时快照是运行时数据，不随发布包回退）
#   - 旧版会用 quant_1.7.db 快照强制替换 quant.db，已移除；如需初始化空库
#     请显式加 -InitQuantDbIfMissing（仅当 quant.db 不存在时生效）
#   - 代码更新后无论是否由计划任务托管，都会真正重启进程加载新代码

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
        # 云端 codeload 实测最稳（12MB 约 1 分钟）；github archive / ghproxy 偶发被截断，
        # 因此每个候选都写临时文件 + 校验 zip 中央目录，确认完整才替换正式包。
        $urls = @(
            "https://codeload.github.com/jikhotel/kronos/zip/refs/tags/$Tag",
            $archive,
            "https://ghproxy.net/$archive"
        )
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $ok = $false
        foreach ($url in $urls) {
            try {
                Write-Host "Downloading : $url"
                $tmp = "$ZipPath.part"
                Remove-Item $tmp -Force -ErrorAction SilentlyContinue
                $prev = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                & curl.exe -L --fail --silent --show-error --connect-timeout 15 --max-time 300 -o $tmp $url 2>&1 | Out-Null
                $ec = $LASTEXITCODE
                $ErrorActionPreference = $prev
                $size = if (Test-Path $tmp) { (Get-Item $tmp).Length } else { 0 }
                if ($ec -ne 0 -or $size -lt 100000) {
                    Write-Warning "download incomplete: $size bytes (exit $ec)"
                    continue
                }
                $z = [IO.Compression.ZipFile]::OpenRead($tmp)
                $entries = $z.Entries.Count
                $z.Dispose()
                if ($entries -lt 20) {
                    Write-Warning "archive invalid: only $entries entries"
                    continue
                }
                Move-Item $tmp $ZipPath -Force
                $ok = $true
                Write-Host "Archive OK  : $size bytes / $entries entries"
                break
            } catch {
                Write-Warning "download failed: $($_.Exception.Message.Split([char]10)[0])"
            }
        }
        if (-not $ok) { throw "Cannot download a valid $Tag archive (codeload / GitHub / ghproxy all failed)" }
    }
    if (-not (Test-Path $ZipPath)) { throw "Zip not found: $ZipPath" }
    Write-Host "Using zip   : $ZipPath"

    $stage = Join-Path $env:TEMP ("kronos_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Expand-Archive -Path $ZipPath -DestinationPath $stage -Force
    $inner = Get-ChildItem -Path $stage -Directory | Select-Object -First 1
    if (-not $inner) { throw "Empty archive" }
    Write-Host "Staged      : $($inner.FullName)"

    # 只更新受版本管理的源码；运行时文件一律保留，避免覆盖线上账号与数据。
    # 排除：所有 *.db / WAL / SHM、密钥、预测历史、行情缓存、日志、备份。
    $n = 0
    Get-ChildItem -LiteralPath $inner.FullName -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($inner.FullName.Length + 1)
        if ($rel -like 'webui\prediction_results\*') { return }
        if ($rel -like 'webui\cache\*') { return }
        if ($rel -like '*.db' -or $rel -like '*.db-wal' -or $rel -like '*.db-shm' -or
            $rel -like '*.db.bak*') { return }
        if ($rel -eq 'webui\.secret_key') { return }
        if ($rel -like '*.log') { return }
        if ($rel -like 'webui\screener_data.json*') { return }
        # 不要用发布包里的旧版助手覆盖正在运行的脚本自身
        if ($rel -eq 'deploy_cloud.ps1') { return }
        $dest = Join-Path $root $rel
        $dir = Split-Path $dest -Parent
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
        $n++
    }
    Write-Host "Copied files: $n (runtime databases preserved)"

    # 线上 quant.db 一律不替换。仅当「目标缺失」且显式要求时才用包内快照初始化，
    # 且优先使用最新快照，避免把 1.7 的旧库灌进新版本。
    $dstDb = Join-Path $root "quant\quant.db"
    if (-not (Test-Path $dstDb) -and $InitQuantDbIfMissing) {
        $snap = @("quant\artifacts\quant_1.8.0.db", "quant\artifacts\quant_1.7.db") |
                ForEach-Object { Join-Path $inner.FullName $_ } |
                Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($snap) {
            Copy-Item $snap $dstDb -Force
            Remove-Item "$dstDb-wal" -Force -ErrorAction SilentlyContinue
            Remove-Item "$dstDb-shm" -Force -ErrorAction SilentlyContinue
            Write-Host "DB initialized from snapshot: $snap"
        }
    } elseif (Test-Path $dstDb) {
        Write-Host "DB preserved: $dstDb"
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

# 无论是否由计划任务托管，代码更新后都必须真正重启，否则服务仍跑旧代码。
$taskName = "Kronos WebUI"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

if ($InstallAutostart -or $task) {
    if (-not $task) {
        $a = New-ScheduledTaskAction -Execute $pyExe -Argument $pyArgs -WorkingDirectory $wd
        $t = New-ScheduledTaskTrigger -AtStartup
        $p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $st = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $taskName -Action $a -Trigger $t -Principal $p -Settings $st -Force | Out-Null
        Write-Host "Autostart   : created scheduled task '$taskName'"
    } else {
        Write-Host "Autostart   : existing scheduled task '$taskName' kept"
    }
    # 先停任务，再停所有残留 app.py 进程，避免双实例抢 7070
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    $procs | ForEach-Object {
        Write-Host "Stopping PID: $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Started via : scheduled task '$taskName'"
} else {
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
