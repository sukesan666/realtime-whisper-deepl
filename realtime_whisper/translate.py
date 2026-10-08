"""DeepL API Free English-to-Japanese translation wrapper."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from collections import deque
from typing import Callable


class ArgosTranslator:
    """Compatibility wrapper that now translates through DeepL API Free."""

    def __init__(self, logger: logging.Logger, status_cb: Callable[[str], None]) -> None:
        self.logger = logger
        self.status_cb = status_cb
        self.ready = False
        self._lock = threading.Lock()
        self._context: deque[str] = deque(maxlen=3)

    @staticmethod
    def _auth_key() -> str:
        key = os.environ.get("DEEPL_AUTH_KEY", "").strip()
        if not key:
            raise RuntimeError("DEEPL_AUTH_KEY is not set")
        return key

    @staticmethod
    def _api_base() -> str:
        return os.environ.get("DEEPL_API_URL", "https://api-free.deepl.com").rstrip("/")

    def _request(self, path: str, *, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._api_base()}{path}",
            data=data,
            method="GET" if data is None else "POST",
            headers={
                "Authorization": f"DeepL-Auth-Key {self._auth_key()}",
                "Content-Type": "application/json",
                "User-Agent": "Realtime-Whisper/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepL API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepL connection failed: {exc.reason}") from exc

    def ensure_ready(self) -> None:
        """Validate the key and connection without consuming translation quota."""
        with self._lock:
            self.status_cb("DeepL API checking...")
            usage = self._request("/v2/usage")
            count = usage.get("character_count")
            limit = usage.get("character_limit")
            self.ready = True
            if isinstance(count, int) and isinstance(limit, int):
                self.status_cb(f"DeepL ready ({count:,}/{limit:,} characters)")
            else:
                self.status_cb("DeepL ready")

    def translate_text(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        payload: dict[str, object] = {
            "text": [text],
            "source_lang": "EN",
            "target_lang": "JA",
        }
        if self._context:
            payload["context"] = " ".join(self._context)
        result = self._request("/v2/translate", payload=payload)
        translations = result.get("translations") or []
        if not translations:
            raise RuntimeError("DeepL returned no translation")
        translated = str(translations[0].get("text", "")).strip()
        self._context.append(text)
        return translated

    def mark_unavailable(self) -> None:
        self.ready = False
