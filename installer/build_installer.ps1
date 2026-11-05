# Builds "Audiobook Forge Setup <version>.exe" from this Audiobook Forge folder.
# Needs Inno Setup 6.5.2 or newer:   winget install --id JRSoftware.InnoSetup -e
$ErrorActionPreference = "Stop"
$app = Split-Path -Parent $PSScriptRoot
$out = Join-Path $PSScriptRoot "Output"
Write-Host "Audiobook Forge folder: $app"

# 1. ffmpeg goes inside the program, so other PCs do not need it installed.
#    Some ffmpeg builds ("shared") are a small .exe that needs DLL files next to it, and winget gives a link to the
#    real file: so find the real folder, copy the .exe AND the DLLs, and check that the copy really runs.
$bin = Join-Path $app "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
foreach ($tool in "ffmpeg", "ffprobe") {
    $found = Get-Command "$tool.exe" -ErrorAction SilentlyContinue
    if (-not $found) { throw "$tool.exe was not found. Install ffmpeg (winget install Gyan.FFmpeg) and run this again." }
    $src = Get-Item $found.Source
    if ($src.LinkType -and $src.Target) { $src = Get-Item ($src.Target | Select-Object -First 1) }
    Copy-Item $src.FullName (Join-Path $bin "$tool.exe") -Force
    Get-ChildItem -Path $src.DirectoryName -Filter *.dll -ErrorAction SilentlyContinue | Copy-Item -Destination $bin -Force
    Write-Host "Copied $tool and its files from $($src.DirectoryName)"
}
& (Join-Path $bin "ffmpeg.exe") -hide_banner -version | Select-Object -First 1
if ($LASTEXITCODE -ne 0) { throw "The copy of ffmpeg in $bin does not start. Install a full ffmpeg build (winget install Gyan.FFmpeg) and run this again." }

# 2. the Inno Setup compiler
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup was not found. Install it with:  winget install --id JRSoftware.InnoSetup -e" }
Write-Host "Using $iscc"

# 3. compile; if the result is too big for one file, build it as Setup.exe + Setup-1.bin instead
$version = "1.0." + (Get-Date -Format "yyyyMMdd")
$iss = Join-Path $PSScriptRoot "AudiobookForge.iss"
$args1 = @("/DAppSrc=$app", "/DAppVersion=$version", "/O$out", $iss)
Write-Host "Compressing about 6 GB. This takes a while (often 15-40 minutes)..."
& $iscc @args1
if ($LASTEXITCODE -ne 0) {
    Write-Host "One file was too big or the build failed; trying again as a setup file plus a data file..."
    & $iscc "/DSPAN" @args1
    if ($LASTEXITCODE -ne 0) { throw "The installer could not be built. See the messages above." }
}
Write-Host ""
Write-Host "Done. The installer is in: $out"
Start-Process explorer.exe $out
