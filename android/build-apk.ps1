# build-apk.ps1 - builds tvapp.apk without Gradle (aapt2 + javac + d8 + apksigner)
#   powershell -ExecutionPolicy Bypass -File .\build-apk.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$sdk = 'C:\Android\Sdk'
$bt  = Join-Path $sdk 'build-tools\36.0.0'      # 34.0.0's d8 crashes on JDK 21
$jar = Join-Path $sdk 'platforms\android-35\android.jar'
$jdk = 'C:\Program Files\Microsoft\jdk-21.0.12.101-hotspot'
$javac   = Join-Path $jdk 'bin\javac.exe'
$keytool = Join-Path $jdk 'bin\keytool.exe'
$jarTool = Join-Path $jdk 'bin\jar.exe'

foreach ($p in @($bt, $jar, $javac)) { if (-not (Test-Path $p)) { throw "missing: $p" } }

# 1. assets
New-Item -ItemType Directory -Force -Path 'assets' | Out-Null
Copy-Item '..\channels.json' 'assets\channels.json' -Force
Copy-Item '..\hls.min.js'    'assets\hls.min.js'    -Force
Write-Host "[1/7] assets: channels.json + hls.min.js + index.html"

# 2. keys
if (-not (Test-Path 'debug.keystore')) {
    & $keytool -genkeypair -v -keystore debug.keystore -alias androiddebugkey `
        -keyalg RSA -keysize 2048 -validity 10000 `
        -storepass android -keypass android `
        -dname "CN=Android Debug,O=Android,C=US" | Out-Null
}
Write-Host "[2/7] keystore ready"

# 3. resources + manifest -> unsigned apk
$unsigned = Join-Path $here 'app-unsigned.apk'
if (Test-Path $unsigned) { Remove-Item $unsigned -Force }
& (Join-Path $bt 'aapt2.exe') link -o $unsigned -I $jar `
    --manifest 'AndroidManifest.xml' -A 'assets' `
    --min-sdk-version 21 --target-sdk-version 34 `
    --version-code 2 --version-name '1.1'
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link failed' }
Write-Host "[3/7] resources linked"

# 4. compile  (Start-Process + one quoted argument string: the paths contain spaces)
if (Test-Path 'classes') { Remove-Item 'classes' -Recurse -Force }
New-Item -ItemType Directory -Force -Path 'classes' | Out-Null
$srcs = @(Get-ChildItem 'src' -Recurse -Filter *.java | ForEach-Object { '"' + $_.FullName + '"' })
$argLine = "-source 8 -target 8 -encoding UTF-8 -nowarn -classpath `"$jar`" -d `"$here\classes`" " + ($srcs -join ' ')
$proc = Start-Process -FilePath $javac -ArgumentList $argLine -Wait -PassThru -NoNewWindow
if ($proc.ExitCode -ne 0) { throw 'javac failed' }
Write-Host "[4/7] java compiled ($($srcs.Count) files)"

# 5. dex  (d8 wants one jar, not a pile of loose .class files)
if (Test-Path 'dexout') { Remove-Item 'dexout' -Recurse -Force }
New-Item -ItemType Directory -Force -Path 'dexout' | Out-Null
& $jarTool cf 'classes.jar' -C 'classes' .
if ($LASTEXITCODE -ne 0) { throw 'jar failed' }
& (Join-Path $bt 'd8.bat') --lib $jar --min-api 21 --output 'dexout' 'classes.jar'
if ($LASTEXITCODE -ne 0) { throw 'd8 failed' }
Write-Host "[5/7] dexed"

# 6. put classes.dex inside the apk
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($unsigned, 'Update')
$old = $zip.GetEntry('classes.dex')
if ($old) { $old.Delete() }
[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (Join-Path $here 'dexout\classes.dex'), 'classes.dex') | Out-Null
$zip.Dispose()
Write-Host "[6/7] classes.dex added"

# 7. align + sign
if (Test-Path 'app-aligned.apk') { Remove-Item 'app-aligned.apk' -Force }
& (Join-Path $bt 'zipalign.exe') -f 4 $unsigned 'app-aligned.apk'
if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }
if (Test-Path 'tvapp.apk') { Remove-Item 'tvapp.apk' -Force }
& (Join-Path $bt 'apksigner.bat') sign --ks 'debug.keystore' --ks-pass 'pass:android' `
    --key-pass 'pass:android' --out 'tvapp.apk' 'app-aligned.apk'
if ($LASTEXITCODE -ne 0) { throw 'apksigner failed' }

$size = [math]::Round((Get-Item 'tvapp.apk').Length / 1MB, 2)
Write-Host "[7/7] signed -> tvapp.apk ($size MB)"
& (Join-Path $bt 'apksigner.bat') verify --print-certs 'tvapp.apk' | Select-Object -First 2
