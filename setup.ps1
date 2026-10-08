$ErrorActionPreference = "Stop"
Set-ExecutionPolicy -Scope Process Bypass -Force | Out-Null

if (Get-Command python -ErrorAction SilentlyContinue) {
    python -m venv .venv
} else {
    py -3 -m venv .venv
}

. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Write-Host "Setup complete. Run .\run.ps1 to start the app."
