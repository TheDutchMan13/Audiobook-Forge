# Rebuilds "Audiobook Forge.exe" (the small launcher, forge_launcher.py) with PyInstaller.
# Only needed when forge_launcher.py or forge.ico change: the window and converter are plain .py files that the
# launcher runs with runtime\python, so changing them never needs a new exe.
# PyInstaller lives in its own folder (C:\forge-build) so it never touches the program's runtime.
$ErrorActionPreference = "Stop"
$app = Split-Path -Parent $PSScriptRoot
$venv = "C:\forge-build"
if (-not (Test-Path "$venv\Scripts\pyinstaller.exe")) {
    Write-Host "Setting up PyInstaller in $venv (one time)..."
    py -3.12 -m venv $venv
    & "$venv\Scripts\python.exe" -m pip install --upgrade pip pyinstaller
}
$work = Join-Path $env:TEMP "forge-exe-build"
& "$venv\Scripts\pyinstaller.exe" --noconfirm --onefile --noconsole --name "Audiobook Forge" `
    --icon (Join-Path $app "forge.ico") --distpath $app --workpath $work --specpath $work (Join-Path $app "forge_launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed; see the messages above." }
Write-Host "Built $(Join-Path $app 'Audiobook Forge.exe')"
