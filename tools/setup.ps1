# ADA Checker setup for Windows 10 and 11. Run by double-clicking Setup.bat.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$Host.UI.RawUI.WindowTitle = 'ADA Checker setup'

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }

function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@('python') }
    foreach ($v in '313', '312', '311', '310') {
        $candidates += ,@("$env:LOCALAPPDATA\Programs\Python\Python$v\python.exe")
        $candidates += ,@("$env:ProgramFiles\Python$v\python.exe")
    }
    foreach ($c in $candidates) {
        $exe = $c[0]
        $pyArgs = @()
        if ($c.Count -gt 1) { $pyArgs = $c[1..($c.Count - 1)] }
        if (($exe -like '*\*') -and -not (Test-Path $exe)) { continue }
        try {
            $ver = & $exe @pyArgs -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match '^3\.(1[0-9])$') { return ,$c }
        } catch { }
    }
    return $null
}

Say ''
Say 'Setting up ADA Checker. The first time takes a few minutes.' 'Cyan'
Say ''

$py = Find-Python
if (-not $py) {
    Say 'Python is not installed. Installing Python 3.12 from Microsoft''s app installer (winget)...' 'Yellow'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $py = Find-Python
    }
}
if (-not $py) {
    Say 'Python could not be installed automatically.' 'Red'
    Say 'Install Python 3.12 from https://www.python.org/downloads/ and check "Add python.exe to PATH", then run Setup again.'
    exit 1
}
$exe = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs = $py[1..($py.Count - 1)] }
Say "Using Python: $(& $exe @pyArgs -c 'import sys,platform;print(sys.version.split()[0], platform.machine())')" 'Green'

$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Say 'Creating a private Python environment for ADA Checker...'
    & $exe @pyArgs -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { Say 'Could not create the environment.' 'Red'; exit 1 }
}

Say 'Installing the checking libraries...'
& $venvPy -m pip install --upgrade pip --disable-pip-version-check --quiet
# Only ready-made builds, so ARM based PCs get the newest version that has an ARM build instead of trying to compile one
& $venvPy -m pip install -r (Join-Path $root 'requirements.txt') --only-binary=:all: --disable-pip-version-check --quiet
if ($LASTEXITCODE -ne 0) { Say 'Installing libraries failed. Check your internet connection and run Setup again.' 'Red'; exit 1 }

# Microsoft Edge runs the web page checks. It comes with Windows 11.
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { Say 'Microsoft Edge was not found. Web page checks will use a simpler built-in check.' 'Yellow' }

Say 'Creating shortcuts...'
$shell = New-Object -ComObject WScript.Shell
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
$script = Join-Path $root 'ADA Checker.pyw'
$icon = Join-Path $root 'adachecker\ui\icon.ico'
$places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('SendTo'))
foreach ($place in $places) {
    $lnk = $shell.CreateShortcut((Join-Path $place 'ADA Checker.lnk'))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = '"' + $script + '"'
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = $icon
    $lnk.Description = 'Check documents for ADA and WCAG accessibility'
    $lnk.Save()
}

Say ''
Say 'ADA Checker is ready.' 'Green'
Say ' - Desktop and Start menu: ADA Checker'
Say ' - In File Explorer: right click any file or folder, Send to, ADA Checker'
Say ''
Start-Process -FilePath $pyw -ArgumentList ('"' + $script + '"') -WorkingDirectory $root
