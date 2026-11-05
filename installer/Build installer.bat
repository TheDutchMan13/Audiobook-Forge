@echo off
title Build the Audiobook Forge installer
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_installer.ps1"
echo.
pause
