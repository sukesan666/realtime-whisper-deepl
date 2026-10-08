"""Headless JSON-Lines bridge between the Tauri app and the whisper pipeline.

The GUI now lives in Tauri (``app-tauri``); this process is the backend it drives
as a *sidecar*. It reuses the same building blocks as the Tkinter UI
(:mod:`realtime_whisper.audio`, :mod:`~.backends`, :mod:`~.translate`,
:mod:`~.textproc`, :mod:`~.config`) but speaks a line protocol instead of drawing
widgets:

* **stdin**  — one JSON command per line (``{"cmd": ...}``).
* **stdout** — one JSON event per line (``{"event": ...}``).

Commands: ``start``, ``stop``, ``set_translation``, ``set_overlay``,
``reload_devices``, ``prepare_whisper``, ``prepare_argos``, ``update_settings``,
``get_state``, ``shutdown``.

Events: ``ready``, ``log``, ``status``, ``transcript``, ``devices``,
``settings``, ``running``, ``error``.

The canonical transcript history (overlap merging, 50-line cap, spill to
``logs/transcript-YYYY-MM-DD.log``) is owned here so the frontend only mirrors it.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import backends
from .audio import (
    DeviceOption,
    LoopbackCapture,
    drop_oldest_put,
    enumerate_loopback_devices,
    normalize_audio_level,
    resample_mono,
)
from .config import (
    COMPUTE_CHOICES,
    DEVICE_CHOICES,
    LOG_DIR,
    LOG_PATH,
    MODEL_CHOICES,
    WHISPER_BACKEND_CHOICES,
    AppConfig,
    ensure_parent_dir,
    load_config,
    save_config,
)
from .textproc import (
    build_initial_prompt,
    clean_transcript_text,
    integrate_transcript,
    normalize_text,
)
from .translate import ArgosTranslator

MAX_TRANSCRIPT_LINES = 50


class JsonLineHandler(logging.Handler):
    """Forward log records to the Tauri frontend as ``{"event": "log"}`` lines."""

    def __init__(self, emit: Any) -> None:
        super().__init__()
        self._emit = emit

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            line = self.format(record)
        except Exception:
            return
        self._emit({"event": "log", "line": line})


class BridgeController:
    def __init__(self) -> None:
        self._out_lock = threading.Lock()
        self.config = load_config()
        self.logger = logging.getLogger("realtime_whisper")
        self.logger.setLevel(logging.INFO)

        self.stop_event = threading.Event()
        self.capture: LoopbackCapture | None = None
        self.whisper_model: Any = None
        self.whisper_runtime: dict[str, Any] | None = None
        self.whisper_lock = threading.Lock()
        self.argos = ArgosTranslator(self.logger, self.set_status)

        self.current_device: DeviceOption | None = None
        self.device_options: list[DeviceOption] = []

        # Canonical, merged transcript state (owned here, mirrored by the UI).
        # japanese_history is index-aligned to english_history ("" = not yet
        # translated); aligning by position (not by text) survives the in-place
        # line rewrites that integrate_transcript performs on overlap merges.
        self.english_history: list[str] = []
        self.japanese_history: list[str] = []
        self.last_english = ""
        self.last_raw_transcript = ""
        self._last_queued_text = ""
        self.prompt_pause_chunks = 0

        self.transcription_queue: queue.Queue[tuple[np.ndarray, int]] | None = None
        self.translation_queue: queue.Queue[str] | None = None

        self._setup_logging()

    # ---- output plumbing --------------------------------------------------
    def emit(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def _setup_logging(self) -> None:
        ensure_parent_dir(LOG_PATH)
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        self.logger.addHandler(file_handler)
        json_handler = JsonLineHandler(self.emit)
        json_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(json_handler)
        self.logger.propagate = False

    def set_status(self, text: str) -> None:
        self.emit({"event": "status", "text": text})

    # ---- state snapshots --------------------------------------------------
    def settings_payload(self) -> dict[str, Any]:
        return self.config.to_dict()

    def devices_payload(self) -> list[dict[str, Any]]:
        return [{"index": opt.index, "label": opt.label} for opt in self.device_options]

    def transcript_payload(self) -> dict[str, Any]:
        english = list(self.english_history)
        japanese = [line for line in self.japanese_history if line]
        return {"event": "transcript", "english": english, "japanese": japanese}

    def emit_transcript(self) -> None:
        self.emit(self.transcript_payload())

    def emit_settings(self) -> None:
        self.emit({"event": "settings", "settings": self.settings_payload()})

    def emit_devices(self) -> None:
        self.emit({"event": "devices", "devices": self.devices_payload()})

    def send_ready(self) -> None:
        self.refresh_devices(quiet=True)
        self.emit(
            {
                "event": "ready",
                "settings": self.settings_payload(),
                "devices": self.devices_payload(),
                "choices": {
                    "backend": WHISPER_BACKEND_CHOICES,
                    "model": MODEL_CHOICES,
                    "deviceMode": DEVICE_CHOICES,
                    "compute": COMPUTE_CHOICES,
                },
            }
        )
        self.logger.info("Bridge ready")
        self.logger.info("Python version: %s", platform.python_version())
        self.logger.info("OS: %s %s", platform.system(), platform.release())
        self.logger.info("Whisper backend: %s", self.config.whisper_backend)
        self.logger.info("Whisper model: %s", self.config.model_name)
        if self.config.translation_enabled:
            self.prepare_argos_async()

    # ---- transcript history + overflow persistence ------------------------
    def _transcript_log_path(self) -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d")
        return LOG_DIR / f"transcript-{stamp}.log"

    def _persist_overflow(self, english_line: str, japanese_line: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        rows = [f"{stamp} [EN] {english_line}"]
        if japanese_line:
            rows.append(f"{stamp} [JA] {japanese_line}")
        try:
            ensure_parent_dir(self._transcript_log_path())
            with self._transcript_log_path().open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(row + "\n")
        except Exception as exc:
            self.logger.error("Failed to persist transcript overflow: %s", exc)

    def _reconcile_japanese(self) -> None:
        """Keep japanese_history the same length as english_history, tail-aligned."""
        el = len(self.english_history)
        jl = len(self.japanese_history)
        if jl < el:
            self.japanese_history += [""] * (el - jl)
        elif jl > el:
            self.japanese_history = self.japanese_history[jl - el :]

    def _integrate_english(self, text: str) -> None:
        before = list(self.english_history)
        # keep_last high so integrate_transcript merges without truncating; we do
        # our own 50-line cap + overflow logging below.
        self.english_history = integrate_transcript(self.english_history, text, keep_last=10_000)
        self._reconcile_japanese()
        if len(self.english_history) > MAX_TRANSCRIPT_LINES:
            n = len(self.english_history) - MAX_TRANSCRIPT_LINES
            en_over = self.english_history[:n]
            ja_over = self.japanese_history[:n]
            self.english_history = self.english_history[n:]
            self.japanese_history = self.japanese_history[n:]
            for en_line, ja_line in zip(en_over, ja_over):
                self._persist_overflow(en_line, ja_line)
        if self.english_history != before:
            self.emit_transcript()

    # ---- devices ----------------------------------------------------------
    def refresh_devices(self, quiet: bool = False) -> None:
        if not quiet:
            self.logger.info("Refreshing WASAPI loopback devices")
        try:
            self.device_options = enumerate_loopback_devices()
        except Exception as exc:
            self.logger.error("Device enumeration failed: %s", exc)
            self.device_options = []
        selected: DeviceOption | None = None
        if self.config.selected_device_index is not None:
            for opt in self.device_options:
                if opt.index == self.config.selected_device_index:
                    selected = opt
                    break
        if selected is None and self.device_options:
            selected = self.device_options[0]
        self.current_device = selected
        if selected is not None:
            self.config.selected_device_index = selected.index
            if not quiet:
                self.logger.info("Selected audio device: %s", selected.label)
        elif not quiet:
            self.logger.warning("No WASAPI loopback devices found")
        self.emit_devices()

    # ---- whisper model ----------------------------------------------------
    def _ensure_whisper_model_sync(self) -> None:
        cfg = self.config
        with self.whisper_lock:
            resolved_exe = cfg.whisper_cpp_executable
            resolved_model = cfg.whisper_cpp_model_path
            if (cfg.whisper_backend or "faster-whisper") == "whisper.cpp":
                resolved_exe = str(backends.resolve_whisper_cpp_executable(cfg.whisper_cpp_executable))
                resolved_model = str(
                    backends.resolve_whisper_cpp_model_path(cfg.model_name, cfg.whisper_cpp_model_path)
                )
            runtime = self.whisper_runtime or {}
            if self.whisper_model is not None and runtime.get("backend") == (cfg.whisper_backend or "faster-whisper"):
                if (
                    runtime.get("model_name") == cfg.model_name
                    and runtime.get("requested_mode") == cfg.device_mode
                    and runtime.get("requested_compute") == cfg.compute_type
                ):
                    if runtime.get("backend") != "whisper.cpp" or (
                        runtime.get("executable") == resolved_exe and runtime.get("model_path") == resolved_model
                    ):
                        return
            self.whisper_model = None
            self.whisper_runtime = None
            self.set_status("Whisper model preparing...")
            self.logger.info("Preparing Whisper model")
            self.whisper_model, self.whisper_runtime = backends.load_whisper_model(
                cfg.whisper_backend,
                cfg.model_name,
                cfg.device_mode,
                cfg.compute_type,
                resolved_exe,
                resolved_model,
                self.logger,
            )
            self.logger.info("Whisper ready with runtime=%s", self.whisper_runtime)
            rt = self.whisper_runtime or {}
            if rt.get("backend") == "whisper.cpp":
                self.set_status(f"Whisper ready with whisper.cpp: {Path(str(rt.get('model_path', '?'))).name}")
            else:
                self.set_status(f"Whisper ready on {rt.get('device', '?')} / {rt.get('compute_type', '?')}")

    def prepare_whisper_async(self) -> None:
        def worker() -> None:
            try:
                self.set_status("Whisper model preparing...")
                self._ensure_whisper_model_sync()
            except Exception as exc:
                self.logger.error("Whisper preparation failed: %s", exc)
                self.set_status(f"Whisper prepare failed: {exc}")

        threading.Thread(target=worker, name="prepare-whisper", daemon=True).start()

    def prepare_argos_async(self) -> None:
        def worker() -> None:
            try:
                self.set_status("DeepL API checking...")
                self.argos.ensure_ready()
            except Exception as exc:
                self.logger.error("DeepL preparation failed: %s", exc)
                self.argos.mark_unavailable()
                self.set_status(f"DeepL check failed, translation OFF: {exc}")
                self.config.translation_enabled = False
                self.emit_settings()

        threading.Thread(target=worker, name="prepare-argos", daemon=True).start()

    # ---- capture / transcription ------------------------------------------
    def start(self) -> None:
        if self.capture is not None:
            self.set_status("Already running")
            return
        self._save_config()
        if self.current_device is None:
            self.refresh_devices()
        if self.current_device is None:
            self.set_status("No WASAPI loopback device is available.")
            self.emit({"event": "error", "text": "No loopback device"})
            return
        self.stop_event.clear()
        self.set_status("Preparing Whisper model...")
        translation_enabled = self.config.translation_enabled

        def starter() -> None:
            try:
                self._ensure_whisper_model_sync()
                if self.stop_event.is_set():
                    return
                if translation_enabled:
                    self.prepare_argos_async()
                self._start_capture()
            except Exception as exc:
                self.logger.error("Start failed: %s", exc)
                self.set_status(f"Start failed: {exc}")
                self.emit({"event": "running", "value": False})

        threading.Thread(target=starter, name="start-prep", daemon=True).start()

    def _start_capture(self) -> None:
        if self.stop_event.is_set() or self.capture is not None or self.current_device is None:
            return
        self.capture = LoopbackCapture(
            device=self.current_device,
            chunk_seconds=self.config.chunk_seconds,
            overlap_seconds=self.config.overlap_seconds,
            on_chunk=self._on_audio_chunk,
            on_error=self._on_background_error,
        )
        try:
            self.capture.start()
            self.set_status("Running")
            self.logger.info("Capture started on %s", self.current_device.label)
            self.emit({"event": "running", "value": True})
        except Exception as exc:
            self.capture = None
            self.set_status(f"Capture failed: {exc}")
            self.logger.error("Capture start error: %s", exc)
            self.emit({"event": "running", "value": False})

    def stop(self) -> None:
        self.stop_event.set()
        if self.capture is not None:
            try:
                self.capture.stop()
            except Exception as exc:
                self.logger.error("Capture stop failed: %s", exc)
            self.capture = None
        for q in (self.translation_queue, self.transcription_queue):
            if q is not None:
                try:
                    while True:
                        q.get_nowait()
                except Exception:
                    pass
        self.set_status("Stopped")
        self.logger.info("Stopped")
        self.emit({"event": "running", "value": False})

    def _on_audio_chunk(self, chunk: np.ndarray, source_rate: int) -> None:
        if self.whisper_model is None or self.stop_event.is_set():
            return
        if self.transcription_queue is None:
            self.transcription_queue = queue.Queue(maxsize=2)
            threading.Thread(target=self._transcription_worker, name="transcription-worker", daemon=True).start()
        drop_oldest_put(self.transcription_queue, (chunk, source_rate))

    def _transcription_worker(self) -> None:
        assert self.transcription_queue is not None
        while not self.stop_event.is_set():
            try:
                chunk, source_rate = self.transcription_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if self.stop_event.is_set():
                break
            try:
                audio16k = normalize_audio_level(resample_mono(chunk, source_rate, 16_000))
                if audio16k.size == 0:
                    continue
                if self.prompt_pause_chunks > 0:
                    self.prompt_pause_chunks -= 1
                    initial_prompt = None
                else:
                    initial_prompt = build_initial_prompt(self.last_english)
                segments, _info = self.whisper_model.transcribe(
                    audio16k,
                    language="en",
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 100, "speech_pad_ms": 0},
                    condition_on_previous_text=False,
                    initial_prompt=initial_prompt,
                    temperature=0.0,
                )
                texts = [getattr(s, "text", "").strip() for s in segments if getattr(s, "text", "").strip()]
                raw_text = " ".join(texts).strip()
                text = clean_transcript_text(raw_text)
                if raw_text and text != raw_text.strip():
                    self.prompt_pause_chunks = max(self.prompt_pause_chunks, 2)
                if not text or len(text) < 4:
                    if raw_text:
                        self.prompt_pause_chunks = max(self.prompt_pause_chunks, 2)
                    continue
                if text == self.last_raw_transcript:
                    continue
                self.last_raw_transcript = text
                if normalize_text(text) == normalize_text(self.last_english):
                    continue
                self.logger.info("Transcript: %s", text)
                self._set_latest_english(text)
            except Exception as exc:
                self.logger.error("Transcription failed: %s", exc)
                self.set_status(f"Transcription error: {exc}")

    def _set_latest_english(self, text: str) -> None:
        cleaned = clean_transcript_text(text)
        if len(cleaned) < 8:
            return
        if cleaned.count(" ") < 2 and len(cleaned) < 18:
            return
        if cleaned.endswith("..."):
            return
        self.last_english = cleaned
        self._integrate_english(cleaned)
        # Translate the *merged* tail line so the result aligns with what is on
        # screen (integrate_transcript may have rewritten `cleaned`).
        if self.config.translation_enabled and self.english_history:
            self._queue_translation(self.english_history[-1])

    def _queue_translation(self, text: str) -> None:
        if not text.strip() or text == self._last_queued_text:
            return
        self._last_queued_text = text
        if self.translation_queue is None:
            self.translation_queue = queue.Queue(maxsize=2)
            threading.Thread(target=self._translation_worker, name="translation-worker", daemon=True).start()
        drop_oldest_put(self.translation_queue, text)

    def _translation_worker(self) -> None:
        assert self.translation_queue is not None
        while not self.stop_event.is_set():
            try:
                text = self.translation_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if self.stop_event.is_set():
                break
            try:
                if not self.argos.ready:
                    self.argos.ensure_ready()
                translated = self.argos.translate_text(text)
                if translated:
                    # Assign to the same english line by position (it may have
                    # extended since we dequeued; align to its current index).
                    try:
                        idx = len(self.english_history) - 1 - self.english_history[::-1].index(text)
                        self.japanese_history[idx] = translated
                    except (ValueError, IndexError):
                        pass
                    self.emit_transcript()
            except Exception as exc:
                self.logger.error("Translation failed: %s", exc)
                self.argos.mark_unavailable()
                self.set_status(f"Translation disabled: {exc}")
                self.config.translation_enabled = False
                self.emit_settings()

    def _on_background_error(self, message: str) -> None:
        self.logger.error(message)
        self.set_status(message)

    # ---- settings ---------------------------------------------------------
    _SETTING_KEYS = {
        "selected_device_index",
        "whisper_backend",
        "model_name",
        "whisper_cpp_executable",
        "whisper_cpp_model_path",
        "device_mode",
        "compute_type",
        "chunk_seconds",
        "overlap_seconds",
        "translation_enabled",
        "overlay_enabled",
        "overlay_position",
        "overlay_size",
    }

    def update_settings(self, patch: dict[str, Any]) -> None:
        changed_translation = "translation_enabled" in patch
        changed_device = "selected_device_index" in patch
        for key, value in patch.items():
            if key in self._SETTING_KEYS:
                setattr(self.config, key, value)
        self._save_config()
        self.emit_settings()
        if changed_device:
            self.refresh_devices(quiet=True)
        if changed_translation:
            if self.config.translation_enabled:
                self.prepare_argos_async()
            else:
                # Clear Japanese so the pane empties immediately when turned off.
                self.japanese_history = [""] * len(self.english_history)
                self._last_queued_text = ""
                self.emit_transcript()

    def _save_config(self) -> None:
        try:
            save_config(self.config)
        except Exception as exc:
            self.logger.error("Failed to save config: %s", exc)

    # ---- command dispatch -------------------------------------------------
    def handle(self, message: dict[str, Any]) -> bool:
        cmd = message.get("cmd")
        if cmd == "start":
            self.start()
        elif cmd == "stop":
            self.stop()
        elif cmd == "set_translation":
            self.update_settings({"translation_enabled": bool(message.get("value"))})
        elif cmd == "set_overlay":
            self.update_settings({"overlay_enabled": bool(message.get("value"))})
        elif cmd == "reload_devices":
            self.refresh_devices()
        elif cmd == "prepare_whisper":
            self.prepare_whisper_async()
        elif cmd == "prepare_argos":
            self.prepare_argos_async()
        elif cmd == "update_settings":
            patch = message.get("settings") or {}
            if isinstance(patch, dict):
                self.update_settings(patch)
        elif cmd == "get_state":
            self.emit_settings()
            self.emit_devices()
            self.emit_transcript()
        elif cmd == "shutdown":
            return False
        else:
            self.logger.warning("Unknown command: %s", cmd)
        return True


def _start_parent_watchdog() -> None:
    """Exit if the parent (Tauri) process dies, so we never orphan the sidecar.

    Tauri's dev watcher hard-kills the app on rebuild, which can bypass the clean
    stdin-EOF shutdown; watching the parent handle guarantees no leftover Python
    processes hold onto the audio device.
    """
    raw_pid = os.environ.get("REALTIME_WHISPER_PARENT_PID", "").strip()
    if not raw_pid or os.name != "nt":
        return
    try:
        pid = int(raw_pid)
    except ValueError:
        return
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x00000000
        INFINITE = 0xFFFFFFFF

        # Declare signatures so the 64-bit HANDLE is not truncated to c_int.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        handle = kernel32.OpenProcess(PROCESS_SYNCHRONIZE, False, pid)
        if not handle:
            return

        def watch() -> None:
            # Only exit when the parent actually terminated (WAIT_OBJECT_0);
            # ignore WAIT_FAILED so a bad handle never kills us spuriously.
            if kernel32.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0:
                os._exit(0)

        threading.Thread(target=watch, name="parent-watchdog", daemon=True).start()
    except Exception:
        pass


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    _start_parent_watchdog()
    controller = BridgeController()
    controller.send_ready()

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except Exception as exc:
                controller.logger.error("Bad command JSON: %s (%s)", raw, exc)
                continue
            if not isinstance(message, dict):
                continue
            try:
                if not controller.handle(message):
                    break
            except Exception as exc:
                controller.logger.error("Command handling failed: %s", exc)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            controller.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
