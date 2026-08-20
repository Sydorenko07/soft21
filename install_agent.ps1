$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$config = Join-Path $root 'config.json'
$configExample = Join-Path $root 'config.example.json'
$runAgent = Join-Path $root 'START\run_agent.cmd'

Write-Host 'Installing Paychain local agent...' -ForegroundColor Cyan
if (-not (Test-Path $python)) {
    py -3.12 -m venv $venv
}
if (-not (Test-Path $config) -and (Test-Path $configExample)) {
    Copy-Item $configExample $config
}
if (-not (Test-Path $runAgent)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $runAgent) -Force | Out-Null
    @'
@echo off
cd /d "%~dp0.."
start "Paychain Control Agent" /min ".venv\Scripts\pythonw.exe" "telegram_app\agent.py"
exit /b 0
'@ | Set-Content -LiteralPath $runAgent -Encoding ascii
}
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $root 'requirements.txt')
& $python -m playwright install chromium

$agent = Join-Path $root 'telegram_app\agent.py'
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Paychain Control Agent.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $runAgent
$shortcut.Arguments = ''
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7
$shortcut.Save()

Start-Process -FilePath 'cmd.exe' -ArgumentList ('/c "' + $runAgent + '"') -WorkingDirectory $root -WindowStyle Normal
Write-Host 'Agent installed and started. Pair the PC from the Telegram Mini App.' -ForegroundColor Green
