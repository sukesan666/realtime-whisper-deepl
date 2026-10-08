$ErrorActionPreference = "Stop"
Set-ExecutionPolicy -Scope Process Bypass -Force | Out-Null

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found. Run .\setup.ps1 first."
    exit 1
}

$pythonw = Join-Path (Get-Location) ".venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-Host "pythonw.exe not found. Run .\setup.ps1 first."
    exit 1
}

Start-Process -FilePath $pythonw -ArgumentList "app.py" -WorkingDirectory (Get-Location) -Wait
