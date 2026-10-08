"""Whisper backends: the whisper.cpp CLI wrapper and faster-whisper loading.

All path/device resolution and model loading lives here as plain functions that
take an explicit ``logger``; UI status reporting stays in the UI layer.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from .config import ROOT_DIR, WHISPER_CPP_ROOTS


def _write_wav16(path: Path, audio: np.ndarray, sample_rate: int = 16_000) -> None:
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        audio = np.zeros(1, dtype=np.float32)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def _parse_whisper_cpp_stdout(output: str) -> str:
    lines: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("system_info:") or lower.startswith("whisper_") or lower.startswith("main:"):
            continue
        if "progress" in lower and "%" in lower:
            continue
        if line.startswith("[") and "]" in line:
            line = line.rsplit("]", 1)[-1].strip()
        line = re.sub(r"^\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*", "", line)
        line = re.sub(r"^\d{2}:\d{2}\s*", "", line)
        if line and not re.fullmatch(r"[\d\W_]+", line):
            lines.append(line)
    return " ".join(lines).strip()


class WhisperCppCliModel:
    def __init__(self, executable: Path, model_path: Path, logger: logging.Logger) -> None:
        self.executable = executable
        self.model_path = model_path
        self.logger = logger

    def transcribe(
        self,
        audio16k: np.ndarray,
        language: str = "en",
        beam_size: int = 5,
        vad_filter: bool = True,
        vad_parameters: dict[str, Any] | None = None,
        condition_on_previous_text: bool = False,
        initial_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> tuple[list[Any], dict[str, Any]]:
        del beam_size, vad_filter, vad_parameters, condition_on_previous_text, initial_prompt, temperature
        with tempfile.TemporaryDirectory(prefix="realtime_whisper_") as tmpdir:
            wav_path = Path(tmpdir) / "chunk.wav"
            out_base = Path(tmpdir) / "chunk"
            _write_wav16(wav_path, audio16k, 16_000)
            cmd = [
                str(self.executable),
                "-m",
                str(self.model_path),
                "-f",
                str(wav_path),
                "-np",
                "-otxt",
                "-of",
                str(out_base),
            ]
            self.logger.info("Running whisper.cpp: %s", " ".join(cmd))
            startupinfo = None
            creationflags = 0
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            stdout = completed.stdout or ""
            if completed.returncode != 0:
                raise RuntimeError(f"whisper.cpp failed with exit code {completed.returncode}: {stdout.strip()}")
            txt_path = out_base.with_suffix(".txt")
            if txt_path.exists():
                text = _parse_whisper_cpp_stdout(txt_path.read_text(encoding="utf-8", errors="replace"))
            else:
                text = _parse_whisper_cpp_stdout(stdout)
            segments = [SimpleNamespace(text=text)] if text else []
            return segments, {
                "backend": "whisper.cpp",
                "executable": str(self.executable),
                "model_path": str(self.model_path),
                "language": language,
                "returncode": completed.returncode,
                "stdout": stdout,
                "txt_path": str(txt_path),
            }


def resolve_device_and_compute(requested_mode: str, requested_compute: str) -> tuple[str, str, bool]:
    requested_mode = requested_mode.strip() or "auto"
    requested_compute = requested_compute.strip() or "auto"
    cuda_available = False
    try:
        import ctranslate2 as ctranslate2_runtime  # type: ignore

        cuda_available = int(getattr(ctranslate2_runtime, "get_cuda_device_count", lambda: 0)()) > 0
    except Exception:
        cuda_available = False
    if requested_mode == "auto":
        device = "cuda" if cuda_available else "cpu"
    elif requested_mode == "cuda":
        device = "cuda"
    else:
        device = "cpu"
    if requested_compute == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    else:
        compute_type = requested_compute
        if device == "cpu" and compute_type in {"float16", "int8_float16"}:
            compute_type = "int8"
    return device, compute_type, cuda_available


def resolve_whisper_cpp_executable(configured_path: str) -> Path:
    candidates: list[Path] = []
    if configured_path.strip():
        candidates.append(Path(configured_path).expanduser())
    for name in ("whisper-cli.exe", "whisper-cli"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    candidates.extend(
        [
            ROOT_DIR / "vulkan" / "bin" / "whisper-cli.exe",
            ROOT_DIR / "vulkan" / "bin" / "whisper-cli",
        ]
    )
    for root in WHISPER_CPP_ROOTS:
        candidates.extend(
            [
                root / "vulkan" / "bin" / "whisper-cli.exe",
                root / "vulkan" / "bin" / "whisper-cli",
                root / "build-vulkan" / "bin" / "whisper-cli.exe",
                root / "build-vulkan" / "bin" / "whisper-cli",
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "whisper.cpp executable not found. Put the Vulkan build under ./whisper.cpp/vulkan/bin/ or set 'whisper_cpp_executable'."
    )


def resolve_whisper_cpp_model_path(model_name: str, configured_path: str) -> Path:
    candidates: list[Path] = []
    if configured_path.strip():
        candidates.append(Path(configured_path).expanduser())
    model_name = model_name.strip() or "small.en"
    raw = Path(model_name)
    if raw.suffix:
        candidates.append(raw)
    else:
        candidates.extend(
            [
                ROOT_DIR / "models" / f"ggml-{model_name}.bin",
                ROOT_DIR / "models" / f"{model_name}.bin",
                ROOT_DIR / "models" / model_name,
                ROOT_DIR / f"ggml-{model_name}.bin",
                ROOT_DIR / f"{model_name}.bin",
                ROOT_DIR / "vulkan" / "models" / f"ggml-{model_name}.bin",
                ROOT_DIR / "vulkan" / "models" / f"{model_name}.bin",
            ]
        )
        for root in WHISPER_CPP_ROOTS:
            candidates.extend(
                [
                    root / "models" / f"ggml-{model_name}.bin",
                    root / "models" / f"{model_name}.bin",
                    root / "models" / model_name,
                    root / "bin" / f"ggml-{model_name}.bin",
                    root / "bin" / f"{model_name}.bin",
                    root / "vulkan" / "models" / f"ggml-{model_name}.bin",
                    root / "vulkan" / "models" / f"{model_name}.bin",
                ]
            )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "whisper.cpp model file not found. Put ggml-*.bin under ./whisper.cpp/models/ or set 'whisper_cpp_model_path'."
    )


def load_faster_whisper_model(
    model_name: str, requested_mode: str, requested_compute: str, logger: logging.Logger
) -> tuple[Any, dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel as WhisperModelRuntime  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"faster-whisper is not installed: {exc}") from exc
    device, compute_type, cuda_available = resolve_device_and_compute(requested_mode, requested_compute)
    logger.info(
        "Loading WhisperModel(model_name=%s, device=%s, compute_type=%s, cuda_available=%s)",
        model_name,
        device,
        compute_type,
        cuda_available,
    )
    try:
        model = WhisperModelRuntime(model_name, device=device, compute_type=compute_type)
        return model, {
            "backend": "faster-whisper",
            "device": device,
            "compute_type": compute_type,
            "cuda_available": cuda_available,
            "model_name": model_name,
            "requested_mode": requested_mode,
            "requested_compute": requested_compute,
        }
    except Exception as exc:
        if device != "cpu":
            logger.warning("CUDA load failed, falling back to CPU: %s", exc)
            model = WhisperModelRuntime(model_name, device="cpu", compute_type="int8")
            return model, {
                "backend": "faster-whisper",
                "device": "cpu",
                "compute_type": "int8",
                "cuda_available": False,
                "model_name": model_name,
                "requested_mode": requested_mode,
                "requested_compute": requested_compute,
                "fallback_reason": str(exc),
            }
        raise


def load_whisper_cpp_model(
    model_name: str, cpp_executable: str, cpp_model_path: str, logger: logging.Logger
) -> tuple[Any, dict[str, Any]]:
    executable = resolve_whisper_cpp_executable(cpp_executable)
    model_path = resolve_whisper_cpp_model_path(model_name, cpp_model_path)
    logger.info("Loading whisper.cpp executable=%s model=%s", executable, model_path)
    model = WhisperCppCliModel(executable=executable, model_path=model_path, logger=logger)
    return model, {
        "backend": "whisper.cpp",
        "executable": str(executable),
        "model_path": str(model_path),
        "model_name": model_name,
        "requested_mode": "cpu",
        "requested_compute": "int8",
    }


def load_whisper_model(
    backend: str,
    model_name: str,
    requested_mode: str,
    requested_compute: str,
    cpp_executable: str,
    cpp_model_path: str,
    logger: logging.Logger,
) -> tuple[Any, dict[str, Any]]:
    backend = (backend or "faster-whisper").strip()
    if backend == "whisper.cpp":
        return load_whisper_cpp_model(model_name, cpp_executable, cpp_model_path, logger)
    model, runtime = load_faster_whisper_model(model_name, requested_mode, requested_compute, logger)
    runtime["requested_backend"] = backend
    return model, runtime
