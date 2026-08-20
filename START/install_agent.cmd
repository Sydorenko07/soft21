@echo off
setlocal
cd /d "%~dp0.."
title Paychain Control - Agent setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\install_agent.ps1"
echo.
pause
