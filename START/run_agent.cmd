@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Agent environment is missing. Run install_agent.cmd first.
  pause
  exit /b 1
)
title Paychain Control Agent
".venv\Scripts\python.exe" "telegram_app\agent.py"
pause
