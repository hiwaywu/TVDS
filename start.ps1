# start.ps1 - launch the tvapp channel browser
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -Port 9000 -NoBrowser
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -Restart
param(
    [int]$Port = 8777,
    [switch]$NoBrowser,
    [switch]$Restart
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Host 'python not found on PATH' -ForegroundColor Red; exit 1 }

# kill anything already bound to the port
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    if ($Restart) {
        $conn | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 900
    } else {
        Write-Host "port $Port already listening - reusing it" -ForegroundColor DarkYellow
    }
}

function Test-Up {
    try { $null = Invoke-WebRequest "http://127.0.0.1:$Port/api/channels" -UseBasicParsing -TimeoutSec 2; return $true }
    catch { return $false }
}

function Start-Detached([string]$exe, [string]$argLine, [string]$workdir) {
    # Win32_Process.Create spawns outside this console's job object, so the server
    # keeps running after the window that launched it is closed.
    try {
        $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine      = ('"{0}" {1}' -f $exe, $argLine)
            CurrentDirectory = $workdir
        }
        if ($r.ReturnValue -eq 0) { return $true }
    } catch { }
    try {
        Start-Process -FilePath $exe -ArgumentList $argLine -WorkingDirectory $workdir -WindowStyle Hidden | Out-Null
        return $true
    } catch {
        return $false
    }
}

$up = Test-Up
if (-not $up) {
    $null = Start-Detached $py ('server.py --port {0}' -f $Port) $here
    for ($i = 0; $i -lt 32; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-Up) { $up = $true; break }
    }
}
if (-not $up) { Write-Host 'server did not come up - run python server.py --port 8777 manually to see the error' -ForegroundColor Red; exit 1 }

$url = "http://127.0.0.1:$Port/"
Write-Host "tvapp ready: $url" -ForegroundColor Green
if (-not $NoBrowser) { Start-Process $url }
