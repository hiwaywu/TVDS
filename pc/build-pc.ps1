# build-pc.ps1 - build tvapp.exe (single file, no console, no Python required)
#   powershell -ExecutionPolicy Bypass -File .\build-pc.ps1
param(
    [string]$OutDir = "$PSScriptRoot\..\release",
    [switch]$KeepBuild
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$root = Split-Path -Parent $here
Set-Location $root

Write-Host "[1/4] icon"
$ico = Join-Path $here 'tvapp.ico'
python -c "from PIL import Image; im=Image.open(r'$root\icon-512.png').convert('RGBA'); im.save(r'$ico', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])"

Write-Host "[2/4] assets"
$data = @(
    'index.html', 'hls.min.js', 'channels.json', 'manifest.json', 'sw.js',
    'icon-512.png', 'server.py'
) | ForEach-Object { "..\$_\;$_\;" + $_.Replace('/', '\') }
# PyInstaller --add-data wants "src;dest"; keep every asset at the bundle root
$addData = @()
foreach ($f in 'index.html', 'hls.min.js', 'channels.json', 'manifest.json', 'sw.js', 'icon-512.png', 'server.py') {
    $addData += @('--add-data', "$root\$f;.")
}

Write-Host "[3/4] pyinstaller"
$name = 'tvapp'
$work = Join-Path $here 'build'
$spec = Join-Path $here 'tvapp.spec'
python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name $name `
    --icon $ico `
    --hidden-import queue `
    --hidden-import http.client `
    --hidden-import io `
    --distpath (Join-Path $here 'dist') `
    --workpath $work `
    --specpath $here `
    @addData `
    (Join-Path $here 'app.py')
if ($LASTEXITCODE -ne 0) { throw 'pyinstaller failed' }

Write-Host "[4/4] publish"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Copy-Item (Join-Path $here "dist\$name.exe") (Join-Path $OutDir "$name.exe") -Force
# ship the catalogue beside the exe: it is preferred over the bundled copy at runtime,
# so channel-list updates never require a rebuild
Copy-Item (Join-Path $root 'channels.json') (Join-Path $OutDir 'channels.json') -Force
Copy-Item (Join-Path $root 'index.html') (Join-Path $OutDir 'index.html') -Force
if (-not $KeepBuild) {
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $here 'dist') -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $spec -Force -ErrorAction SilentlyContinue
}

$exe = Join-Path $OutDir "$name.exe"
$size = [math]::Round((Get-Item $exe).Length / 1MB, 2)
Write-Host "built $exe ($size MB)"
