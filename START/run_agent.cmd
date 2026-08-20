@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Agent environment is missing. Run install_agent.cmd first.
  pause
  exit /b 1
)
title Paychain Control Agent Launcher
start "Paychain Control Agent" /min ".venv\Scripts\pythonw.exe" "telegram_app\agent.py"
exit /b 0
