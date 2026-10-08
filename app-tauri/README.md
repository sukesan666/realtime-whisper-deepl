# Realtime Whisper — Tauri frontend

Rust + Tauri frontend for the DeepL variant of Realtime Whisper. The Python
sidecar handles WASAPI audio capture, local Whisper transcription and DeepL API
Free translation. See the [main README](../README.md) for setup and data handling.

## Build and run

First create the Python `.venv` at the repository root, install the Python
requirements, and configure your own `DEEPL_AUTH_KEY` environment variable.
From this `app-tauri` directory:

```powershell
npm.cmd ci
npm.cmd run tauri build -- --no-bundle
powershell -ExecutionPolicy Bypass -File ..\run-tauri.ps1
```

The executable is `src-tauri/target/release/app-tauri.exe`. Use the launcher so
that the Python sidecar is resolved relative to the repository. The launcher
does not rebuild an existing executable after source edits.

For native development, after installing dependencies:

```powershell
npm.cmd run tauri dev
```

`npm.cmd run dev` starts a browser preview with mock data, not live transcription.

## Architecture

- `src/backend.ts`: frontend interface, mock browser backend and Tauri commands.
- `src-tauri/src/lib.rs`: starts the Python sidecar and relays JSON Lines events.
- `../realtime_whisper/bridge.py`: audio/transcription/translation pipeline.
- `overlay.html` and `src/overlay.ts`: Japanese-only subtitle overlay with white
  text, a black outline, transparent background and Noto Sans JP at 36px bold.

Some internal command/class names still contain `argos` for compatibility.
Translation in this variant uses DeepL, not Argos.

## Local settings and data

`REALTIME_WHISPER_ROOT` and `REALTIME_WHISPER_PYTHON` can override the repository
and Python paths. `run-tauri.ps1` sets them for its process. API keys, personal
settings, transcript logs, build output and model files are not distributed.
The backend reads `DEEPL_AUTH_KEY` from the user's environment.
