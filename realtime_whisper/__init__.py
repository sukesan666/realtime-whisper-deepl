"""Realtime Whisper — local WASAPI-loopback speech-to-text application package.

The former single-file ``app.py`` has been split into focused modules:

- :mod:`realtime_whisper.config`    — paths, choices and :class:`AppConfig`
- :mod:`realtime_whisper.textproc`  — pure transcript cleaning / merging helpers
- :mod:`realtime_whisper.audio`     — WASAPI loopback capture and resampling
- :mod:`realtime_whisper.backends`  — whisper.cpp / faster-whisper loading
- :mod:`realtime_whisper.translate` — Argos en->ja wrapper
- :mod:`realtime_whisper.overlay`   — top-most subtitle overlay window
- :mod:`realtime_whisper.ui`        — the Tkinter application
"""

APP_VERSION = "0.2.0"
