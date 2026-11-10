# Removes the shortcuts and the private Python environment. Your documents and reports are not touched.
$root = Split-Path -Parent $PSScriptRoot
foreach ($place in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('SendTo'))) {
    Remove-Item (Join-Path $place 'ADA Checker.lnk') -ErrorAction SilentlyContinue
}
Remove-Item (Join-Path $root '.venv') -Recurse -Force -ErrorAction SilentlyContinue
Write-Host 'Shortcuts and program files removed. You can now delete this folder.'
Write-Host "Your history is in $env:LOCALAPPDATA\ADA Checker and your reports are where you saved them; delete those too if you want."
