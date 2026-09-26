"""ANSI colors for terminal output.

Colors are used only when the stream is a terminal. ``NO_COLOR`` (any
non-empty value) turns them off and ``FORCE_COLOR`` turns them on even when
the output is redirected; ``NO_COLOR`` wins if both are set.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


def _enable_windows_vt(stream: TextIO) -> bool:
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(mode.value & enable_vt) or bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except (ImportError, AttributeError, OSError, ValueError):
        return False


def supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    return sys.platform != "win32" or _enable_windows_vt(stream)


def paint(text: str, *styles: str, stream: TextIO | None = None) -> str:
    """Wrap ``text`` in the given styles if ``stream`` (default: stdout) supports color."""
    if not styles or not supports_color(stream or sys.stdout):
        return text
    return f"\033[{';'.join(_CODES[s] for s in styles)}m{text}\033[0m"
