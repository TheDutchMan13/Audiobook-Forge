@echo off
setlocal
title Audiobook Forge
cd /d "%~dp0"

if "%~1"=="" (
    echo Drag one or more .epub files onto this file to convert them to audiobooks.
    echo.
    pause
    exit /b
)

for %%F in (%*) do (
    echo ============================================================
    echo Converting: %%~nxF
    echo ============================================================
    "%~dp0runtime\python\python.exe" convert.py "%%~fF" 2>nul
    echo.
)

echo All done. Audiobooks are in: %~dp0output
echo.
pause
