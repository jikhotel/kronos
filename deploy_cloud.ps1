param(
    [string]$ProjectRoot = "",
    [string]$ZipPath = "",
    [string]$Tag = "v1.7",
    [switch]$RestartOnly
)

# Kronos cloud deployment helper for Windows Server.
# Run in an elevated PowerShell from the cloud console (VNC/RDP):
#   powershell -ExecutionPolicy Bypass -File .\deploy_cloud.ps1
# Or, if the zip was copied to the server manually:
#   powershell -ExecutionPolicy Bypass -File .\deploy_cloud.ps1 -ZipPath C:\Temp\kronos-v1.7.zip

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
        $url = "https://github.com/jikhotel/kronos/archive/refs/tags/$Tag.zip"
        Write-Host "Downloading : $url"
        Invoke-WebRequest -Uri $url -OutFile $ZipPath -UseBasicParsing -TimeoutSec 180
    }
    if (-not (Test-Path $ZipPath)) { throw "Zip not found: $ZipPath" }
    Write-Host "Using zip   : $ZipPath"

    $stage = Join-Path $env:TEMP ("kronos_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Expand-Archive -Path $ZipPath -DestinationPath $stage -Force
    $inner = Get-ChildItem -Path $stage -Directory | Select-Object -First 1
    if (-not $inner) { throw "Empty archive" }
    Write-Host "Staged      : $($inner.FullName)"

    # Copy tracked source files only. Runtime files (users.db, .secret_key,
    # quant.db, model cache, prediction_results) are intentionally left alone.
    $n = 0
    Get-ChildItem -Path $inner.FullName -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($inner.FullName.Length + 1)
        $dest = Join-Path $root $rel
        $dir = Split-Path $dest -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item $_.FullName $dest -Force
        $n++
    }
    Write-Host "Copied files: $n"

    # Replace the quant database with the 1.7 snapshot (441-backtest artifacts).
    $srcDb = Join-Path $root "quant\artifacts\quant_1.7.db"
    $dstDb = Join-Path $root "quant\quant.db"
    if (Test-Path $srcDb) {
        if (Test-Path $dstDb) {
            $bak = "$dstDb.bak_" + (Get-Date -Format "yyyyMMdd_HHmmss")
            Copy-Item $dstDb $bak -Force
            Write-Host "DB backup   : $bak"
        }
        Copy-Item $srcDb $dstDb -Force
        # Stale WAL/SHM would overwrite the freshly installed database.
        Remove-Item "$dstDb-wal" -Force -ErrorAction SilentlyContinue
        Remove-Item "$dstDb-shm" -Force -ErrorAction SilentlyContinue
        Write-Host "DB updated  : $dstDb"
    } else {
        Write-Warning "quant_1.7.db not found in package; existing quant.db kept"
    }

    $ver = Get-Content (Join-Path $root "VERSION") -Raw
    Write-Host "VERSION     : $($ver.Trim())"
}

# Restart the Flask service, preserving the original interpreter and arguments.
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -match 'app\.py' })
$pyExe = "python.exe"
$pyArgs = "app.py"
if ($procs.Count -gt 0) {
    $p = $procs[0]
    if ($p.ExecutablePath) { $pyExe = $p.ExecutablePath }
    $idx = $p.CommandLine.IndexOf("app.py")
    if ($idx -ge 0) { $pyArgs = $p.CommandLine.Substring($idx) }
    $procs | ForEach-Object {
        Write-Host "Stopping PID: $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}
$wd = Join-Path $root "webui"
$outLog = Join-Path $wd "service_stdout.log"
$errLog = Join-Path $wd "service_stderr.log"
Write-Host "Starting    : $pyExe $pyArgs (cwd $wd)"
Start-Process -FilePath $pyExe -ArgumentList $pyArgs -WorkingDirectory $wd `
    -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Start-Sleep -Seconds 8

foreach ($port in @(7070, 80)) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 6
        Write-Host "CHECK OK    : port $port -> HTTP $($r.StatusCode)"
    } catch {
        Write-Host "CHECK FAIL  : port $port -> $($_.Exception.Message.Split([char]10)[0])"
    }
}
Write-Host "Done. Logs: $outLog / $errLog"
