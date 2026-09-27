# Start the assistant silently (tray icon only, no console) when you sign in.
# Usage (PowerShell):  powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1
# Remove it again:      powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1 -Remove
param([switch]$Remove, [switch]$ShowHud)
$root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'PC Assistant.lnk'
if ($Remove) {
    Remove-Item $link -ErrorAction SilentlyContinue
    Write-Host "Removed $link"
    exit 0
}
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    Write-Error "Run setup.bat first ($pythonw not found)."
    exit 1
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $pythonw          # pythonw = no console window
$shortcut.Arguments = if ($ShowHud) { '-m assistant' } else { '-m assistant --no-window' }
$shortcut.WorkingDirectory = $root
$shortcut.Description = 'Voice PC assistant (tray)'
$shortcut.Save()
Write-Host "Installed: $link  (logs: $root\data\logs\assistant.log)"
