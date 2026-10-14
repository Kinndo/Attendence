# Creates a shortcut in your Startup folder so the bot launches when you sign in.
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File install_startup.ps1
# Remove it again with:  powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove
param([switch]$Remove)

$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'Attendance Bot.lnk'

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed $link" } else { Write-Host "Nothing to remove." }
    exit 0
}

$runBat = Join-Path $PSScriptRoot 'run.bat'
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $runBat
$sc.WorkingDirectory = $PSScriptRoot
$sc.WindowStyle = 7   # minimised
$sc.Description = 'Teams attendance poll bot'
$sc.Save()
Write-Host "Created $link"
Write-Host "The bot will start minimised the next time you sign in to Windows."
