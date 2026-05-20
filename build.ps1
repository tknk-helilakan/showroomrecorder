$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "Missing .venv. Create it and install dependencies before building."
}

$BuildHome = Join-Path $Root ".pyinstaller-home"
$AppData = Join-Path $BuildHome "AppData\Roaming"
$LocalAppData = Join-Path $BuildHome "AppData\Local"
New-Item -ItemType Directory -Force -Path $AppData, $LocalAppData | Out-Null

$env:HOME = $BuildHome
$env:USERPROFILE = $BuildHome
$env:APPDATA = $AppData
$env:LOCALAPPDATA = $LocalAppData
$env:PYTHONUTF8 = "1"

& .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm .\showroomrecorder.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "Build complete: dist\showroomrecorder\showroomrecorder.exe"
Write-Host "Keep config.yaml, models\, data\, and ffmpeg outside the exe."
