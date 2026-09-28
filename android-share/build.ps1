$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$sdk = Join-Path $PSScriptRoot '../tools/android-sdk'
$bt = Join-Path $sdk 'build-tools/34.0.0'
$platform = Join-Path $sdk 'platforms/android-34/android.jar'
$env:JAVA_HOME = 'C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot'
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
$out = Join-Path $PSScriptRoot ('build/native-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path "$out/gen", "$out/obj", "$out/dex" -Force | Out-Null
function Check { if ($LASTEXITCODE -ne 0) { throw "Build failed: $LASTEXITCODE" } }
& "$bt/aapt2.exe" compile --dir res -o "$out/res.zip"
Check
& "$bt/aapt2.exe" link -o "$out/base.apk" -I $platform --manifest AndroidManifest.xml --java "$out/gen" --auto-add-overlay "$out/res.zip"
Check
$sources = @(Get-ChildItem src -Recurse -Filter '*.java' | ForEach-Object FullName)
& javac --release 11 -encoding UTF-8 -classpath $platform -d "$out/obj" @sources
Check
$classes = @(Get-ChildItem "$out/obj" -Recurse -Filter '*.class' | ForEach-Object FullName)
& "$bt/d8.bat" --lib $platform --release --min-api 24 --output "$out/dex" @classes
Check
Copy-Item "$out/base.apk" "$out/unsigned.apk"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open("$out/unsigned.apk", [IO.Compression.ZipArchiveMode]::Update)
try { [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "$out/dex/classes.dex", 'classes.dex') | Out-Null }
finally { $zip.Dispose() }
& "$bt/zipalign.exe" -f 4 "$out/unsigned.apk" "$out/aligned.apk"
Check
& "$bt/apksigner.bat" sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android --out "$out/VideoMindShare.apk" "$out/aligned.apk"
Check
& "$bt/apksigner.bat" verify "$out/VideoMindShare.apk"
Check
Write-Output "APK=$out/VideoMindShare.apk"
