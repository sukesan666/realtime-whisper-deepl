"""WASAPI loopback capture, device enumeration and audio resampling helpers."""

from __future__ import annotations

import queue
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover
    import pyaudio  # type: ignore


@dataclass
class DeviceOption:
    index: int
    name: str
    sample_rate: int
    channels: int

    @property
    def label(self) -> str:
        return f"{self.index}: {self.name} [{self.channels}ch {self.sample_rate}Hz]"


def resample_mono(audio: np.ndarray, source_rate: int, target_rate: int = 16_000) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0 or source_rate == target_rate:
        return audio.astype(np.float32, copy=False)
    duration = audio.shape[0] / float(source_rate)
    target_size = max(1, int(round(duration * target_rate)))
    src_x = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    tgt_x = np.linspace(0.0, duration, num=target_size, endpoint=False)
    return np.interp(tgt_x, src_x, audio).astype(np.float32)


def normalize_audio_level(audio: np.ndarray, target_rms: float = 0.08, max_gain: float = 4.0) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return audio
    rms = float(np.sqrt(np.mean(np.square(audio))))
    if not np.isfinite(rms) or rms <= 1e-6:
        return audio
    gain = min(max_gain, max(0.5, target_rms / rms))
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32, copy=False)


def drain_deque_to_samples(buffers: deque[np.ndarray]) -> np.ndarray:
    if not buffers:
        return np.empty(0, dtype=np.float32)
    if len(buffers) == 1:
        return buffers[0]
    return np.concatenate(list(buffers)).astype(np.float32, copy=False)


def trim_buffers(buffers: deque[np.ndarray], target_samples: int) -> int:
    total = sum(int(chunk.shape[0]) for chunk in buffers)
    while buffers and total > target_samples:
        first = buffers[0]
        if total - first.shape[0] >= target_samples:
            total -= int(first.shape[0])
            buffers.popleft()
            continue
        keep = target_samples - (total - int(first.shape[0]))
        if keep <= 0:
            buffers.popleft()
            total = target_samples
            break
        buffers[0] = first[-keep:]
        total = target_samples
        break
    return total


def drop_oldest_put(q: queue.Queue[Any], item: Any) -> None:
    """Put *item*, discarding the oldest entry if the bounded queue is full."""
    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


def enumerate_loopback_devices() -> list[DeviceOption]:
    devices: list[DeviceOption] = []
    pa = pyaudio.PyAudio()
    try:
        if hasattr(pa, "get_loopback_device_info_generator"):
            for info in pa.get_loopback_device_info_generator():
                try:
                    index = int(info["index"])
                    devices.append(
                        DeviceOption(
                            index=index,
                            name=str(info.get("name", f"Device {index}")),
                            sample_rate=int(info.get("defaultSampleRate", 48000)),
                            channels=int(info.get("maxInputChannels", 2) or info.get("maxOutputChannels", 2) or 2),
                        )
                    )
                except Exception:
                    continue
        elif hasattr(pa, "get_default_wasapi_loopback"):
            info = pa.get_default_wasapi_loopback()
            devices.append(
                DeviceOption(
                    index=int(info["index"]),
                    name=str(info.get("name", "Default loopback")),
                    sample_rate=int(info.get("defaultSampleRate", 48000)),
                    channels=int(info.get("maxInputChannels", 2) or 2),
                )
            )
    finally:
        try:
            pa.terminate()
        except Exception:
            pass
    devices.sort(key=lambda item: item.label.lower())
    return devices


class LoopbackCapture:
    def __init__(
        self,
        device: DeviceOption,
        chunk_seconds: float,
        overlap_seconds: float,
        on_chunk: Callable[[np.ndarray, int], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.device = device
        self.chunk_seconds = max(0.5, float(chunk_seconds))
        self.overlap_seconds = max(0.0, min(float(overlap_seconds), self.chunk_seconds - 0.1))
        self.on_chunk = on_chunk
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.raw_queue: queue.Queue[bytes] = queue.Queue(maxsize=24)
        self.pa = pyaudio.PyAudio()
        self.stream: Any = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        frames_per_buffer = max(512, int(self.device.sample_rate * 0.05))
        self.stream = self.pa.open(
            format=pyaudio.paFloat32,
            channels=max(1, self.device.channels),
            rate=self.device.sample_rate,
            input=True,
            input_device_index=self.device.index,
            frames_per_buffer=frames_per_buffer,
            stream_callback=self._callback,
        )
        self.stream.start_stream()
        self.thread = threading.Thread(target=self._worker, name="loopback-capture", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.raw_queue.put_nowait(b"")
        except queue.Full:
            pass
        if self.stream is not None:
            try:
                if self.stream.is_active():
                    self.stream.stop_stream()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None
        try:
            self.pa.terminate()
        except Exception:
            pass

    def _callback(self, in_data: bytes, frame_count: int, time_info: dict[str, Any], status: int):
        if self.stop_event.is_set():
            return (None, pyaudio.paComplete)
        drop_oldest_put(self.raw_queue, in_data)
        return (None, pyaudio.paContinue)

    def _worker(self) -> None:
        step_seconds = max(0.65, min(self.chunk_seconds - self.overlap_seconds, 1.0))
        step_samples = max(1, int(round(step_seconds * self.device.sample_rate)))
        chunk_samples = max(step_samples, int(round(self.chunk_seconds * self.device.sample_rate)))
        context_seconds = max(self.chunk_seconds * 5.0, self.chunk_seconds + 12.0, 16.0)
        context_samples = max(chunk_samples, int(round(context_seconds * self.device.sample_rate)))
        buffers: deque[np.ndarray] = deque()
        total_samples = 0
        samples_since_emit = 0

        while not self.stop_event.is_set() or not self.raw_queue.empty():
            try:
                raw = self.raw_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if self.stop_event.is_set() and not raw:
                break
            if not raw:
                continue
            try:
                block = np.frombuffer(raw, dtype=np.float32)
                if block.size == 0:
                    continue
                if self.device.channels > 1:
                    frame_count = block.size // self.device.channels
                    if frame_count <= 0:
                        continue
                    block = block[: frame_count * self.device.channels].reshape(frame_count, self.device.channels).mean(axis=1)
                buffers.append(block.astype(np.float32, copy=False))
                total_samples += int(block.shape[0])
                samples_since_emit += int(block.shape[0])
                total_samples = trim_buffers(buffers, max(context_samples, chunk_samples + step_samples))
                if samples_since_emit < step_samples or total_samples < chunk_samples:
                    continue
                chunk = drain_deque_to_samples(buffers)
                if chunk.shape[0] < chunk_samples:
                    continue
                try:
                    self.on_chunk(chunk.copy(), self.device.sample_rate)
                except Exception as exc:
                    self.on_error(f"Audio chunk dispatch failed: {exc}")
                samples_since_emit = 0
                total_samples = trim_buffers(buffers, context_samples)
            except Exception as exc:
                self.on_error(f"Audio processing failed: {exc}")
