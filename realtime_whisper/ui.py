"""The Tkinter application that wires audio capture, Whisper and translation."""

from __future__ import annotations

import logging
import platform
import queue
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from . import backends
from .audio import DeviceOption, LoopbackCapture, drop_oldest_put, enumerate_loopback_devices, normalize_audio_level, resample_mono
from .config import (
    APP_TITLE,
    COMPUTE_CHOICES,
    DEVICE_CHOICES,
    LOG_PATH,
    MODEL_CHOICES,
    WHISPER_BACKEND_CHOICES,
    AppConfig,
    ensure_parent_dir,
    load_config,
    safe_float,
    save_config,
)
from .overlay import OverlayWindow
from .textproc import build_initial_prompt, clean_transcript_text, integrate_transcript, normalize_text
from .translate import ArgosTranslator

APP_INSTANCE: "RealtimeWhisperApp"


class GuiLogHandler(logging.Handler):
    def __init__(self, enqueue_ui: Callable[[Callable[[], None]], None]) -> None:
        super().__init__()
        self.enqueue_ui = enqueue_ui

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return
        self.enqueue_ui(lambda msg=message: APP_INSTANCE.append_log(msg))


class RealtimeWhisperApp:
    def __init__(self) -> None:
        global APP_INSTANCE
        APP_INSTANCE = self

        self.config = load_config()
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("980x760")
        self.root.minsize(900, 680)
        self.root.state("normal")
        self.root.deiconify()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.logger = logging.getLogger("realtime_whisper")
        self.logger.setLevel(logging.INFO)
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=500)
        self.stop_event = threading.Event()
        self.capture: LoopbackCapture | None = None
        self.whisper_model: Any = None
        self.whisper_runtime: dict[str, Any] | None = None
        self.whisper_lock = threading.Lock()
        self.argos = ArgosTranslator(self.logger, self.set_status)
        self.last_english = ""
        self.last_translated = ""
        self.last_raw_transcript = ""
        self.latest_logged_error = ""
        self.prompt_pause_chunks = 0
        self.current_device: DeviceOption | None = None
        self.device_options: list[DeviceOption] = []
        self._config_save_after_id: str | None = None
        self.translation_queue: queue.Queue[str] | None = None
        self.transcription_queue: queue.Queue[tuple[np.ndarray, int]] | None = None
        self.overlay = OverlayWindow(self)

        self._setup_logging()
        self.logger.info("Tk bootstrap starting")
        self._build_ui()
        self.logger.info("Tk UI built")
        self._bind_hotkeys()
        self._apply_config_to_widgets()
        self.refresh_devices()
        self._apply_overlay_state(initial=True)
        self._drain_ui_queue()

        self.logger.info("Python version: %s", platform.python_version())
        self.logger.info("OS: %s %s", platform.system(), platform.release())
        self.logger.info("Whisper backend: %s", self.config.whisper_backend)
        self.logger.info("Whisper model: %s", self.config.model_name)
        self.logger.info("whisper.cpp exe: %s", self.config.whisper_cpp_executable or "<auto>")
        self.logger.info("whisper.cpp model: %s", self.config.whisper_cpp_model_path or "<auto>")
        self.logger.info("Initial device mode: %s", self.config.device_mode)
        self.logger.info("Initial compute type: %s", self.config.compute_type)

        # If translation was left ON, warm up Argos in the background at launch so
        # the first line does not stall waiting for the en->ja model to prepare.
        if self.config.translation_enabled:
            self.prepare_argos_model_async()

    def _setup_logging(self) -> None:
        ensure_parent_dir(LOG_PATH)
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        self.logger.addHandler(file_handler)
        gui_handler = GuiLogHandler(self.enqueue_ui)
        gui_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(gui_handler)
        self.logger.propagate = False

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=12)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(top, text="Whisper backend").grid(row=row, column=0, sticky="w", pady=3)
        self.backend_var = tk.StringVar(value=self.config.whisper_backend)
        self.backend_combo = ttk.Combobox(top, textvariable=self.backend_var, values=WHISPER_BACKEND_CHOICES, state="readonly")
        self.backend_combo.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 8))
        ttk.Button(top, text="Prepare Whisper", command=self.prepare_whisper_model_async).grid(row=row, column=2, sticky="ew", pady=3)

        row += 1
        ttk.Label(top, text="Input device").grid(row=row, column=0, sticky="w", pady=3)
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(top, textvariable=self.device_var, state="readonly", width=58)
        self.device_combo.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 8))
        ttk.Button(top, text="Reload", command=self.refresh_devices).grid(row=row, column=2, sticky="ew", pady=3)

        row += 1
        ttk.Label(top, text="Whisper model").grid(row=row, column=0, sticky="w", pady=3)
        self.model_var = tk.StringVar(value=self.config.model_name)
        self.model_combo = ttk.Combobox(top, textvariable=self.model_var, values=MODEL_CHOICES, state="readonly")
        self.model_combo.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 8))

        row += 1
        ttk.Label(top, text="whisper.cpp exe").grid(row=row, column=0, sticky="w", pady=3)
        self.whisper_cpp_exe_var = tk.StringVar(value=self.config.whisper_cpp_executable)
        ttk.Entry(top, textvariable=self.whisper_cpp_exe_var).grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 8))
        ttk.Label(top, text="blank = auto detect").grid(row=row, column=2, sticky="w", pady=3)

        row += 1
        ttk.Label(top, text="whisper.cpp model").grid(row=row, column=0, sticky="w", pady=3)
        self.whisper_cpp_model_var = tk.StringVar(value=self.config.whisper_cpp_model_path)
        ttk.Entry(top, textvariable=self.whisper_cpp_model_var).grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 8))
        ttk.Label(top, text="blank = auto from model").grid(row=row, column=2, sticky="w", pady=3)

        row += 1
        ttk.Label(top, text="Device mode").grid(row=row, column=0, sticky="w", pady=3)
        self.device_mode_var = tk.StringVar(value=self.config.device_mode)
        self.device_mode_combo = ttk.Combobox(top, textvariable=self.device_mode_var, values=DEVICE_CHOICES, state="readonly")
        self.device_mode_combo.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 8))
        ttk.Label(top, text="Compute type").grid(row=row, column=2, sticky="w", pady=3, padx=(8, 0))

        row += 1
        ttk.Label(top, text="Chunk seconds").grid(row=row, column=0, sticky="w", pady=3)
        self.chunk_var = tk.StringVar(value=str(self.config.chunk_seconds))
        ttk.Entry(top, textvariable=self.chunk_var).grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 8))
        self.compute_type_var = tk.StringVar(value=self.config.compute_type)
        self.compute_type_combo = ttk.Combobox(top, textvariable=self.compute_type_var, values=COMPUTE_CHOICES, state="readonly")
        self.compute_type_combo.grid(row=row, column=2, sticky="ew", pady=3)

        row += 1
        ttk.Label(top, text="Overlap seconds").grid(row=row, column=0, sticky="w", pady=3)
        self.overlap_var = tk.StringVar(value=str(self.config.overlap_seconds))
        ttk.Entry(top, textvariable=self.overlap_var).grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 8))
        self.translate_enabled_var = tk.BooleanVar(value=self.config.translation_enabled)
        ttk.Checkbutton(top, text="Translation ON", variable=self.translate_enabled_var, command=self._on_translation_toggle).grid(row=row, column=2, sticky="w", pady=3)

        row += 1
        ttk.Button(top, text="Check DeepL en→ja", command=self.prepare_argos_model_async).grid(row=row, column=0, sticky="ew", pady=3)
        self.overlay_enabled_var = tk.BooleanVar(value=self.config.overlay_enabled)
        ttk.Checkbutton(top, text="Topmost subtitle overlay", variable=self.overlay_enabled_var, command=self._on_overlay_toggle).grid(row=row, column=1, sticky="w", pady=3, padx=(8, 8))
        button_row = ttk.Frame(top)
        button_row.grid(row=row, column=2, sticky="ew", pady=3)
        ttk.Button(button_row, text="Start", command=self.start).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(button_row, text="Stop", command=self.stop).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(8, 0))

        body = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(4, weight=1)
        body.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(body, textvariable=self.status_var, anchor="w").grid(row=0, column=0, sticky="ew", pady=(6, 8))

        ttk.Label(body, text="English").grid(row=1, column=0, sticky="w")
        self.english_text = tk.Text(body, height=5, wrap="word")
        self.english_text.grid(row=2, column=0, sticky="nsew", pady=(4, 10))
        self.english_text.configure(state="disabled")

        ttk.Label(body, text="Japanese").grid(row=3, column=0, sticky="w")
        self.japanese_text = tk.Text(body, height=5, wrap="word")
        self.japanese_text.grid(row=4, column=0, sticky="nsew", pady=(4, 10))
        self.japanese_text.configure(state="disabled")

        ttk.Label(body, text="Log").grid(row=5, column=0, sticky="w")
        self.log_text = scrolledtext.ScrolledText(body, height=12, wrap="word")
        self.log_text.grid(row=6, column=0, sticky="nsew", pady=(4, 0))
        self.log_text.configure(state="disabled")

        for var in [
            self.backend_var,
            self.device_var,
            self.model_var,
            self.whisper_cpp_exe_var,
            self.whisper_cpp_model_var,
            self.device_mode_var,
            self.compute_type_var,
            self.chunk_var,
            self.overlap_var,
            self.translate_enabled_var,
            self.overlay_enabled_var,
        ]:
            var.trace_add("write", lambda *_: self.schedule_config_save())

    def _apply_config_to_widgets(self) -> None:
        self.backend_var.set(self.config.whisper_backend)
        self.model_var.set(self.config.model_name)
        self.whisper_cpp_exe_var.set(self.config.whisper_cpp_executable)
        self.whisper_cpp_model_var.set(self.config.whisper_cpp_model_path)
        self.device_mode_var.set(self.config.device_mode)
        self.compute_type_var.set(self.config.compute_type)
        self.chunk_var.set(str(self.config.chunk_seconds))
        self.overlap_var.set(str(self.config.overlap_seconds))
        self.translate_enabled_var.set(self.config.translation_enabled)
        self.overlay_enabled_var.set(self.config.overlay_enabled)

    def _collect_config_from_widgets(self) -> AppConfig:
        device_index = self.current_device.index if self.current_device else None
        if self.device_var.get():
            try:
                device_index = int(self.device_var.get().split(":", 1)[0])
            except Exception:
                pass
        return AppConfig(
            selected_device_index=device_index,
            whisper_backend=self.backend_var.get().strip() or "whisper.cpp",
            model_name=self.model_var.get().strip() or "small.en",
            whisper_cpp_executable=self.whisper_cpp_exe_var.get().strip(),
            whisper_cpp_model_path=self.whisper_cpp_model_var.get().strip(),
            device_mode=self.device_mode_var.get().strip() or "auto",
            compute_type=self.compute_type_var.get().strip() or "auto",
            chunk_seconds=safe_float(self.chunk_var.get(), 4.0),
            overlap_seconds=safe_float(self.overlap_var.get(), 1.5),
            translation_enabled=bool(self.translate_enabled_var.get()),
            overlay_enabled=bool(self.overlay_enabled_var.get()),
            overlay_position=list(self.config.overlay_position),
            overlay_size=list(self.config.overlay_size),
        )

    def schedule_config_save(self) -> None:
        if self._config_save_after_id is not None:
            try:
                self.root.after_cancel(self._config_save_after_id)
            except Exception:
                pass
        self._config_save_after_id = self.root.after(400, self._save_config_now)

    def _save_config_now(self) -> None:
        self._config_save_after_id = None
        self.config = self._collect_config_from_widgets()
        try:
            save_config(self.config)
        except Exception as exc:
            self.logger.error("Failed to save config: %s", exc)

    def enqueue_ui(self, callback: Callable[[], None]) -> None:
        drop_oldest_put(self.ui_queue, callback)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                pass
        self.root.after(100, self._drain_ui_queue)

    def set_status(self, text: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.enqueue_ui(lambda msg=text: self.set_status(msg))
            return
        self.status_var.set(text)

    def append_log(self, message: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.enqueue_ui(lambda msg=message: self.append_log(msg))
            return
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def set_english(self, text: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.enqueue_ui(lambda msg=text: self.set_english(msg))
            return
        if not hasattr(self, "english_history"):
            self.english_history = []
        text = clean_transcript_text(text)
        if not text:
            return
        self.english_history = integrate_transcript(self.english_history, text)
        self.english_text.configure(state="normal")
        self.english_text.delete("1.0", tk.END)
        self.english_text.insert(tk.END, "\n".join(self.english_history))
        self.english_text.see(tk.END)
        self.english_text.configure(state="disabled")
        self.overlay.set_text(text, self.last_translated)

    def set_japanese(self, text: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.enqueue_ui(lambda msg=text: self.set_japanese(msg))
            return
        self.japanese_text.configure(state="normal")
        self.japanese_text.delete("1.0", tk.END)
        self.japanese_text.insert(tk.END, text)
        self.japanese_text.configure(state="disabled")
        self.overlay.set_text(self.last_english, text)

    def refresh_devices(self) -> None:
        self.logger.info("Refreshing WASAPI loopback devices")
        self.device_options = enumerate_loopback_devices()
        self.device_combo["values"] = [opt.label for opt in self.device_options]
        selected = None
        if self.config.selected_device_index is not None:
            for opt in self.device_options:
                if opt.index == self.config.selected_device_index:
                    selected = opt
                    break
        if selected is None and self.device_options:
            selected = self.device_options[0]
        if selected is None:
            self.current_device = None
            self.device_var.set("")
            self.set_status("No loopback device found")
            self.logger.warning("No WASAPI loopback devices found")
            return
        self.current_device = selected
        self.device_var.set(selected.label)
        self.logger.info("Selected audio device: %s", selected.label)

    def _ensure_whisper_model_sync(
        self,
        backend: str,
        model_name: str,
        requested_mode: str,
        requested_compute: str,
        cpp_executable: str,
        cpp_model_path: str,
    ) -> None:
        with self.whisper_lock:
            resolved_cpp_executable = cpp_executable
            resolved_cpp_model_path = cpp_model_path
            if (backend or "faster-whisper") == "whisper.cpp":
                resolved_cpp_executable = str(backends.resolve_whisper_cpp_executable(cpp_executable))
                resolved_cpp_model_path = str(backends.resolve_whisper_cpp_model_path(model_name, cpp_model_path))
            runtime = self.whisper_runtime or {}
            if self.whisper_model is not None and runtime.get("backend") == (backend or "faster-whisper"):
                if runtime.get("model_name") == model_name and runtime.get("requested_mode") == requested_mode and runtime.get("requested_compute") == requested_compute:
                    if runtime.get("backend") != "whisper.cpp" or (
                        runtime.get("executable") == resolved_cpp_executable and runtime.get("model_path") == resolved_cpp_model_path
                    ):
                        return
            if self.whisper_model is not None:
                self.whisper_model = None
                self.whisper_runtime = None
                runtime = {}
            self.set_status("Whisper model preparing...")
            self.logger.info("Preparing Whisper model")
            self.whisper_model, self.whisper_runtime = backends.load_whisper_model(
                backend,
                model_name,
                requested_mode,
                requested_compute,
                resolved_cpp_executable,
                resolved_cpp_model_path,
                self.logger,
            )
            self.logger.info("Whisper ready with runtime=%s", self.whisper_runtime)
            runtime = self.whisper_runtime or {}
            if runtime.get("backend") == "whisper.cpp":
                self.set_status(f"Whisper ready with whisper.cpp: {Path(str(runtime.get('model_path', '?'))).name}")
            else:
                self.set_status(f"Whisper ready on {runtime.get('device', '?')} / {runtime.get('compute_type', '?')}")

    def prepare_whisper_model_async(self) -> None:
        backend = self.backend_var.get().strip() or "whisper.cpp"
        model_name = self.model_var.get().strip() or "small.en"
        requested_mode = self.device_mode_var.get().strip() or "auto"
        requested_compute = self.compute_type_var.get().strip() or "auto"
        cpp_executable = self.whisper_cpp_exe_var.get().strip()
        cpp_model_path = self.whisper_cpp_model_var.get().strip()

        def worker() -> None:
            try:
                self.enqueue_ui(lambda: self.set_status("Whisper model preparing..."))
                self._ensure_whisper_model_sync(
                    backend,
                    model_name,
                    requested_mode,
                    requested_compute,
                    cpp_executable,
                    cpp_model_path,
                )
            except Exception as exc:
                self.logger.error("Whisper preparation failed: %s", exc)
                self.enqueue_ui(lambda msg=str(exc): self.set_status(f"Whisper prepare failed: {msg}"))

        threading.Thread(target=worker, name="prepare-whisper", daemon=True).start()

    def prepare_argos_model_async(self) -> None:
        def worker() -> None:
            try:
                self.enqueue_ui(lambda: self.set_status("DeepL API checking..."))
                self.argos.ensure_ready()
            except Exception as exc:
                self.logger.error("DeepL preparation failed: %s", exc)
                self.argos.mark_unavailable()
                self.enqueue_ui(lambda msg=str(exc): self.set_status(f"DeepL check failed, translation OFF: {msg}"))
                self.enqueue_ui(lambda: self.translate_enabled_var.set(False))

        threading.Thread(target=worker, name="prepare-argos", daemon=True).start()

    def _on_translation_toggle(self) -> None:
        self.config.translation_enabled = bool(self.translate_enabled_var.get())
        self.schedule_config_save()
        if self.translate_enabled_var.get():
            self.prepare_argos_model_async()
        else:
            self.set_japanese("")

    def _on_overlay_toggle(self) -> None:
        self.config.overlay_enabled = bool(self.overlay_enabled_var.get())
        self.schedule_config_save()
        self._apply_overlay_state()

    def _apply_overlay_state(self, initial: bool = False) -> None:
        if self.overlay_enabled_var.get():
            self.overlay.show()
            if initial and self.config.overlay_position and self.config.overlay_size and self.overlay.window is not None:
                try:
                    x, y = self.config.overlay_position
                    w, h = self.config.overlay_size
                    self.overlay.window.geometry(f"{w}x{h}+{x}+{y}")
                except Exception:
                    pass
        else:
            self.overlay.hide()
        self.overlay.set_text(self.last_english, self.last_translated)

    def _set_latest_english(self, text: str) -> None:
        cleaned = clean_transcript_text(text)
        if len(cleaned) < 8:
            return
        if cleaned.count(" ") < 2 and len(cleaned) < 18:
            return
        if cleaned.endswith("..."):
            return
        self.last_english = cleaned
        self.set_english(cleaned)
        if self.translate_enabled_var.get() and cleaned:
            self._queue_translation(cleaned)

    def _queue_translation(self, text: str) -> None:
        if not text.strip():
            return
        if normalize_text(text) == normalize_text(self.last_translated):
            return
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
                if translated and normalize_text(translated) != normalize_text(self.last_translated):
                    self.last_translated = translated
                    self.enqueue_ui(lambda msg=translated: self.set_japanese(msg))
            except Exception as exc:
                self.logger.error("Translation failed: %s", exc)
                self.argos.mark_unavailable()
                self.enqueue_ui(lambda msg=str(exc): self.set_status(f"Translation disabled: {msg}"))
                self.enqueue_ui(lambda: self.translate_enabled_var.set(False))
                self.enqueue_ui(lambda: self.set_japanese(""))

    def start(self) -> None:
        if self.capture is not None:
            self.set_status("Already running")
            return
        self._save_config_now()
        if self.current_device is None:
            self.refresh_devices()
        if self.current_device is None:
            messagebox.showerror("No device", "No WASAPI loopback device is available.")
            return
        self.stop_event.clear()
        self.set_status("Preparing Whisper model...")
        backend = self.backend_var.get().strip() or "whisper.cpp"
        model_name = self.model_var.get().strip() or "small.en"
        requested_mode = self.device_mode_var.get().strip() or "auto"
        requested_compute = self.compute_type_var.get().strip() or "auto"
        cpp_executable = self.whisper_cpp_exe_var.get().strip()
        cpp_model_path = self.whisper_cpp_model_var.get().strip()
        translation_enabled = bool(self.translate_enabled_var.get())

        def starter() -> None:
            try:
                self._ensure_whisper_model_sync(
                    backend,
                    model_name,
                    requested_mode,
                    requested_compute,
                    cpp_executable,
                    cpp_model_path,
                )
                if self.stop_event.is_set():
                    return
                if translation_enabled:
                    self.prepare_argos_model_async()
                self.enqueue_ui(self._start_capture)
            except Exception as exc:
                self.logger.error("Start failed: %s", exc)
                self.enqueue_ui(lambda msg=str(exc): self.set_status(f"Start failed: {msg}"))

        threading.Thread(target=starter, name="start-prep", daemon=True).start()

    def _start_capture(self) -> None:
        if self.stop_event.is_set() or self.capture is not None or self.current_device is None:
            return
        self.capture = LoopbackCapture(
            device=self.current_device,
            chunk_seconds=safe_float(self.chunk_var.get(), 4.0),
            overlap_seconds=safe_float(self.overlap_var.get(), 1.5),
            on_chunk=self._on_audio_chunk,
            on_error=self._on_background_error,
        )
        try:
            self.capture.start()
            self.set_status("Running")
            self.logger.info("Capture started on %s", self.current_device.label)
        except Exception as exc:
            self.capture = None
            self.set_status(f"Capture failed: {exc}")
            self.logger.error("Capture start error: %s", exc)

    def stop(self) -> None:
        self.stop_event.set()
        if self.capture is not None:
            try:
                self.capture.stop()
            except Exception as exc:
                self.logger.error("Capture stop failed: %s", exc)
            self.capture = None
        if self.translation_queue is not None:
            try:
                while True:
                    self.translation_queue.get_nowait()
            except Exception:
                pass
        if self.transcription_queue is not None:
            try:
                while True:
                    self.transcription_queue.get_nowait()
            except Exception:
                pass
        self.set_status("Stopped")
        self.logger.info("Stopped")

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
                audio16k = resample_mono(chunk, source_rate, 16_000)
                audio16k = normalize_audio_level(audio16k)
                if audio16k.size == 0:
                    continue
                if self.prompt_pause_chunks > 0:
                    self.prompt_pause_chunks -= 1
                    initial_prompt = None
                else:
                    initial_prompt = build_initial_prompt(self.last_english)
                segments, info = self.whisper_model.transcribe(
                    audio16k,
                    language="en",
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 100, "speech_pad_ms": 0},
                    condition_on_previous_text=False,
                    initial_prompt=initial_prompt,
                    temperature=0.0,
                )
                texts: list[str] = []
                for segment in segments:
                    segment_text = getattr(segment, "text", "").strip()
                    if segment_text:
                        texts.append(segment_text)
                raw_text = " ".join(texts).strip()
                text = clean_transcript_text(raw_text)
                if raw_text and text != raw_text.strip():
                    self.prompt_pause_chunks = max(self.prompt_pause_chunks, 2)
                if not text or len(text) < 2:
                    if raw_text:
                        self.prompt_pause_chunks = max(self.prompt_pause_chunks, 2)
                    continue
                if len(text) < 4:
                    continue
                if text == self.last_raw_transcript:
                    continue
                self.last_raw_transcript = text
                if normalize_text(text) == normalize_text(self.last_english):
                    continue
                self.logger.info("Transcript: %s", text)
                self.enqueue_ui(lambda msg=text: self._set_latest_english(msg))
            except Exception as exc:
                self.logger.error("Transcription failed: %s", exc)
                self.enqueue_ui(lambda msg=str(exc): self.set_status(f"Transcription error: {msg}"))

    def _on_background_error(self, message: str) -> None:
        if message == self.latest_logged_error:
            return
        self.latest_logged_error = message
        self.logger.error(message)
        self.enqueue_ui(lambda msg=message: self.set_status(msg))

    def _bind_hotkeys(self) -> None:
        self.root.bind("<Control-Return>", lambda _e: self.start())
        self.root.bind("<Escape>", lambda _e: self.stop())

    def on_close(self) -> None:
        self.stop()
        self._save_config_now()
        self.overlay.destroy()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _ensure_root_visible(self) -> None:
        try:
            self.root.state("normal")
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(800, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def run(self) -> None:
        try:
            self.logger.info("Entering Tk mainloop")
            self.root.update_idletasks()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(600, lambda: self.root.attributes("-topmost", False))
            self.root.after(100, self._ensure_root_visible)
        except Exception:
            pass
        self.root.mainloop()


def main() -> None:
    app = RealtimeWhisperApp()
    app.run()
