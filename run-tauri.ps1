# Launch the Tauri (Rust + WebView2) frontend of Realtime Whisper.
#
# This runs the compiled release build directly (no dev server), which starts the
# main window plus the always-on-top subtitle overlay, and auto-spawns the Python
# backend bridge (realtime_whisper.bridge) from the local .venv.
#
# On first run (or if the exe is missing) it builds the release binary via
# `cargo tauri build --no-bundle`. Subsequent runs launch instantly.

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$AppDir = Join-Path $Root "app-tauri"
$Exe = Join-Path $AppDir "src-tauri\target\release\app-tauri.exe"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

# Make cargo/rustc available in this session if they are not already on PATH.
$CargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
if (Test-Path $CargoBin) { $env:Path = "$CargoBin;$env:Path" }

if (-not (Test-Path $Python)) {
    Write-Error "Python venv not found at $Python. Run .\setup.ps1 first."
}

if (-not (Test-Path $Exe)) {
    Write-Host "Release build not found - building (first run only, this takes a few minutes)..."
    Push-Location $AppDir
    try {
        cmd /c "npm run tauri build -- --no-bundle"
        if ($LASTEXITCODE -ne 0) { Write-Error "Build failed." }
    } finally {
        Pop-Location
    }
}

# Point the app at this repo's backend explicitly so it works regardless of the
# working directory it is launched from.
$env:REALTIME_WHISPER_ROOT = $Root
$env:REALTIME_WHISPER_PYTHON = $Python

Write-Host "Starting Realtime Whisper..."
& $Exe
