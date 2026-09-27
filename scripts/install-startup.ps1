# Start the assistant automatically when you sign in to Windows.
# Usage (PowerShell):  powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1
# Remove it again:      powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1 -Remove
param([switch]$Remove)
$root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'PC Assistant.lnk'
if ($Remove) {
    Remove-Item $link -ErrorAction SilentlyContinue
    Write-Host "Removed $link"
    exit 0
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = Join-Path $root 'start.bat'
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7   # minimized console
$shortcut.Description = 'Voice PC assistant + HUD'
$shortcut.Save()
Write-Host "Installed: $link"
