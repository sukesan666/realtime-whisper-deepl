"""Top-most subtitle overlay window."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ui import RealtimeWhisperApp


class OverlayWindow:
    def __init__(self, app: "RealtimeWhisperApp") -> None:
        self.app = app
        self.window: tk.Toplevel | None = None
        self.english_var = tk.StringVar(value="")
        self.japanese_var = tk.StringVar(value="")

    def is_open(self) -> bool:
        return self.window is not None and bool(self.window.winfo_exists())

    def show(self) -> None:
        if self.is_open():
            self.window.deiconify()
            self.window.lift()
            self.window.attributes("-topmost", True)
            return
        self.window = tk.Toplevel(self.app.root)
        self.window.title("Subtitle Overlay")
        self.window.configure(bg="#0b0b0b")
        self.window.attributes("-topmost", True)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.minsize(280, 120)
        x, y = self.app.config.overlay_position
        w, h = self.app.config.overlay_size
        self.window.geometry(f"{w}x{h}+{x}+{y}")
        frame = ttk.Frame(self.window, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        self.english_label = tk.Label(
            frame,
            textvariable=self.english_var,
            bg="#0b0b0b",
            fg="#f3f3f3",
            anchor="w",
            justify="left",
            font=("Segoe UI", 12, "bold"),
            wraplength=w - 30,
        )
        self.english_label.pack(fill=tk.BOTH, expand=True)
        self.japanese_label = tk.Label(
            frame,
            textvariable=self.japanese_var,
            bg="#0b0b0b",
            fg="#d7e9ff",
            anchor="w",
            justify="left",
            font=("Segoe UI", 11),
            wraplength=w - 30,
        )
        self.japanese_label.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.window.bind("<Configure>", self._on_configure)

    def hide(self) -> None:
        if self.is_open():
            self.window.withdraw()

    def destroy(self) -> None:
        if self.is_open():
            try:
                self.window.destroy()
            except Exception:
                pass
        self.window = None

    def set_text(self, english: str, japanese: str) -> None:
        self.english_var.set(english)
        self.japanese_var.set(japanese)

    def _on_configure(self, event: tk.Event) -> None:
        if not self.is_open() or self.window.state() == "withdrawn":
            return
        try:
            x = int(self.window.winfo_x())
            y = int(self.window.winfo_y())
            w = int(self.window.winfo_width())
            h = int(self.window.winfo_height())
            self.app.config.overlay_position = [x, y]
            self.app.config.overlay_size = [w, h]
            self.app.schedule_config_save()
            self.english_label.configure(wraplength=max(160, w - 30))
            self.japanese_label.configure(wraplength=max(160, w - 30))
        except Exception:
            pass
