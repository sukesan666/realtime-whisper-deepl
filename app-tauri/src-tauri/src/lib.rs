use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;

use tauri::{Emitter, Manager, State};

/// Handle to the running Python bridge sidecar.
#[derive(Default)]
struct BackendProcess {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
}

type BackendState = Mutex<BackendProcess>;

// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

/// Append overflowed transcript lines (beyond the on-screen 50-line window) to
/// `<app_log_dir>/transcript-<YYYY-MM-DD>.log`. Called by the mock frontend when
/// history spills. `date` is the frontend's local calendar date (YYYY-MM-DD).
///
/// Note: under the real Python sidecar the bridge writes its own dated transcript
/// log (`logs/transcript-YYYY-MM-DD.log`), so this command only backs the mock.
#[tauri::command]
fn append_transcript_log(
    app: tauri::AppHandle,
    lines: Vec<String>,
    date: String,
) -> Result<(), String> {
    if lines.is_empty() {
        return Ok(());
    }
    // Guard the filename: only accept a plain YYYY-MM-DD date (no path separators).
    let safe_date = if date.len() == 10 && date.chars().all(|c| c.is_ascii_digit() || c == '-') {
        date
    } else {
        "unknown".to_string()
    };
    let dir = app.path().app_log_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let path = dir.join(format!("transcript-{safe_date}.log"));
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| e.to_string())?;
    for line in lines {
        writeln!(file, "{line}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Send one JSON-Lines command to the Python bridge's stdin (e.g. start/stop,
/// toggles, settings). Responses arrive asynchronously as `backend-event`s.
#[tauri::command]
fn send_backend_command(state: State<BackendState>, message: String) -> Result<(), String> {
    let mut guard = state.lock().map_err(|e| e.to_string())?;
    let stdin = guard
        .stdin
        .as_mut()
        .ok_or_else(|| "backend not running".to_string())?;
    writeln!(stdin, "{message}").map_err(|e| e.to_string())?;
    stdin.flush().map_err(|e| e.to_string())?;
    Ok(())
}

/// Walk up from the current dir to find the project root (holds `realtime_whisper/bridge.py`).
fn find_project_root() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("REALTIME_WHISPER_ROOT") {
        return Some(PathBuf::from(p));
    }
    let mut dir = std::env::current_dir().ok()?;
    loop {
        if dir.join("realtime_whisper").join("bridge.py").exists() {
            return Some(dir);
        }
        if !dir.pop() {
            return None;
        }
    }
}

/// Resolve the Python interpreter (project venv by default).
fn python_exe(root: &Path) -> PathBuf {
    if let Ok(p) = std::env::var("REALTIME_WHISPER_PYTHON") {
        return PathBuf::from(p);
    }
    #[cfg(windows)]
    {
        root.join(".venv").join("Scripts").join("python.exe")
    }
    #[cfg(not(windows))]
    {
        root.join(".venv").join("bin").join("python")
    }
}

/// Spawn the Python bridge and relay its stdout JSON lines as `backend-event`.
/// The child self-terminates via a parent-PID watchdog (see bridge.py) so it is
/// never orphaned even when the dev watcher hard-kills the app.
fn spawn_backend(app: &tauri::AppHandle) -> Result<(), String> {
    let root = find_project_root().ok_or_else(|| "project root not found".to_string())?;
    let py = python_exe(&root);

    let mut command = Command::new(&py);
    command
        .args(["-u", "-m", "realtime_whisper.bridge"])
        .current_dir(&root)
        .env("REALTIME_WHISPER_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Don't flash a console window for the Python child on Windows.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = command
        .spawn()
        .map_err(|e| format!("failed to launch {}: {e}", py.display()))?;

    let stdout = child.stdout.take().ok_or_else(|| "no stdout".to_string())?;
    let stderr = child.stderr.take();
    let stdin = child.stdin.take();

    // stdout → forward each JSON line to the webview.
    let app_out = app.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            match line {
                Ok(l) => {
                    let l = l.trim();
                    if l.is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<serde_json::Value>(l) {
                        Ok(val) => {
                            let _ = app_out.emit("backend-event", val);
                        }
                        Err(_) => {
                            let _ = app_out.emit(
                                "backend-event",
                                serde_json::json!({ "event": "log", "line": l }),
                            );
                        }
                    }
                }
                Err(_) => break,
            }
        }
        let _ = app_out.emit("backend-event", serde_json::json!({ "event": "exit" }));
    });

    // stderr → console (aids debugging; not shown in the UI).
    if let Some(stderr) = stderr {
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                eprintln!("[bridge stderr] {line}");
            }
        });
    }

    let state: State<BackendState> = app.state();
    let mut guard = state.lock().map_err(|e| e.to_string())?;
    guard.child = Some(child);
    guard.stdin = stdin;
    Ok(())
}

// Windows: main app window + always-on-top subtitle overlay window (see tauri.conf.json).
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage::<BackendState>(Mutex::new(BackendProcess::default()))
        .invoke_handler(tauri::generate_handler![
            greet,
            append_transcript_log,
            send_backend_command
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            if let Err(err) = spawn_backend(&handle) {
                eprintln!("failed to spawn python backend: {err}");
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the MAIN window quits the whole app — otherwise the frameless
            // always-on-top overlay window keeps the process (and sidecar) alive.
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if window.label() == "main" {
                    let app = window.app_handle();
                    // Stop the sidecar (also guarded by its parent-PID watchdog).
                    if let Some(state) = app.try_state::<BackendState>() {
                        if let Ok(mut guard) = state.lock() {
                            guard.stdin.take();
                            if let Some(mut child) = guard.child.take() {
                                let _ = child.kill();
                            }
                        }
                    }
                    app.exit(0);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
