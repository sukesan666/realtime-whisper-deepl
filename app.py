"""Entry point for Realtime Whisper.

The implementation now lives in the :mod:`realtime_whisper` package; this module
stays so ``run.ps1`` / ``launch_gui.bat`` can keep launching ``app.py``.
"""

from __future__ import annotations

from realtime_whisper.ui import main

if __name__ == "__main__":
    main()
