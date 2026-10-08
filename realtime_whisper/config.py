"""Application paths, UI choices and the persisted :class:`AppConfig`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

APP_TITLE = "Realtime Whisper"

# ``config.py`` lives in ``<root>/realtime_whisper/``; the project root is one
# directory up. Every path below is anchored to that root so the app behaves the
# same regardless of the current working directory.
ROOT_DIR = Path(__file__).resolve().parent.parent
LOCAL_STATE_DIR = ROOT_DIR / ".state"
ARGOS_DATA_DIR = LOCAL_STATE_DIR / "argos"
ARGOS_CONFIG_DIR = LOCAL_STATE_DIR / "argos-config"
ARGOS_CACHE_DIR = LOCAL_STATE_DIR / "argos-cache"

CONFIG_PATH = ROOT_DIR / "config.json"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.json"
LOG_DIR = ROOT_DIR / "logs"
LOG_PATH = LOG_DIR / "app.log"

DEFAULT_CONFIG: dict[str, Any] = {
    "selected_device_index": None,
    "whisper_backend": "whisper.cpp",
    "model_name": "small.en",
    "whisper_cpp_executable": "",
    "whisper_cpp_model_path": "",
    "device_mode": "auto",
    "compute_type": "auto",
    "chunk_seconds": 4.0,
    "overlap_seconds": 1.5,
    "translation_enabled": False,
    "overlay_enabled": False,
    "overlay_position": [80, 80],
    "overlay_size": [420, 180],
}

MODEL_CHOICES = ["tiny.en", "base.en", "small.en", "medium.en", "distil-large-v3"]
WHISPER_BACKEND_CHOICES = ["faster-whisper", "whisper.cpp"]
DEVICE_CHOICES = ["auto", "cuda", "cpu"]
COMPUTE_CHOICES = ["auto", "float16", "int8_float16", "int8"]
WHISPER_CPP_ROOTS = [
    ROOT_DIR / "whisper.cpp",
    ROOT_DIR / "tools" / "whisper.cpp",
    ROOT_DIR / "external" / "whisper.cpp",
]


@dataclass
class AppConfig:
    selected_device_index: Optional[int] = None
    whisper_backend: str = "whisper.cpp"
    model_name: str = "small.en"
    whisper_cpp_executable: str = ""
    whisper_cpp_model_path: str = ""
    device_mode: str = "auto"
    compute_type: str = "auto"
    chunk_seconds: float = 4.0
    overlap_seconds: float = 1.5
    translation_enabled: bool = False
    overlay_enabled: bool = False
    overlay_position: list[int] = field(default_factory=lambda: [80, 80])
    overlay_size: list[int] = field(default_factory=lambda: [420, 180])

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        data = dict(DEFAULT_CONFIG)
        data.update(raw or {})
        pos = data.get("overlay_position") or [80, 80]
        size = data.get("overlay_size") or [420, 180]
        return cls(
            selected_device_index=data.get("selected_device_index"),
            whisper_backend=str(data.get("whisper_backend", "whisper.cpp")),
            model_name=str(data.get("model_name", "small.en")),
            whisper_cpp_executable=str(data.get("whisper_cpp_executable", "")),
            whisper_cpp_model_path=str(data.get("whisper_cpp_model_path", "")),
            device_mode=str(data.get("device_mode", "auto")),
            compute_type=str(data.get("compute_type", "auto")),
            chunk_seconds=float(data.get("chunk_seconds", 4.0)),
            overlap_seconds=float(data.get("overlap_seconds", 1.5)),
            translation_enabled=bool(data.get("translation_enabled", False)),
            overlay_enabled=bool(data.get("overlay_enabled", False)),
            overlay_position=[int(pos[0]), int(pos[1])],
            overlay_size=[int(size[0]), int(size[1])],
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["overlay_position"] = [int(self.overlay_position[0]), int(self.overlay_position[1])]
        data["overlay_size"] = [int(self.overlay_size[0]), int(self.overlay_size[1])]
        return data


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    if not path.exists():
        return AppConfig.from_dict(DEFAULT_CONFIG)
    try:
        return AppConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return AppConfig.from_dict(DEFAULT_CONFIG)


def save_config(config: AppConfig) -> None:
    ensure_parent_dir(CONFIG_PATH)
    CONFIG_PATH.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def safe_float(value: str, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default
