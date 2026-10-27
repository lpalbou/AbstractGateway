"""Console output that never crashes on a character the terminal's code page lacks.

On Windows a redirected or piped stream (``> file``, ``| Select-String``, a CI log) uses the
ANSI code page, often cp1252, which has no arrows, emoji or box-drawing characters. The
command-line entry points call `safe_stdio()` first, so such a character is written as a
replacement character instead of ending the command with ``UnicodeEncodeError``. UTF-8
streams and interactive consoles are left as they are.
"""

from __future__ import annotations

import sys


def safe_stdio() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = str(getattr(stream, "encoding", "") or "").lower().replace("_", "-")
        if encoding in {"utf-8", "utf8", "utf-8-sig"}:
            continue
        try:
            reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - a stream that cannot be reconfigured keeps its behavior
            pass


def hide_child_consoles() -> bool:
    """Start every child process without a console window when this process has no console.

    Under ``pythonw.exe`` (the login entry and the tray) there is no console for a child to
    share, so Windows gives each console program its own window: a GPU poll through
    ``nvidia-smi`` every few seconds flashed a window on the desktop. Adding
    ``CREATE_NO_WINDOW`` to every ``subprocess.Popen`` (``asyncio`` subprocesses included)
    keeps them hidden. A caller that asks for its own console or a detached process keeps
    it. Returns whether the default was installed.
    """

    import os

    if os.name != "nt":
        return False
    import ctypes
    import subprocess

    try:
        if ctypes.windll.kernel32.GetConsoleCP():
            return False  # a console exists (even a hidden or windowless one): children share it
    except Exception:  # noqa: BLE001
        return False
    if getattr(subprocess.Popen, "_abstractgateway_no_window", False):
        return True
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    own_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10) | getattr(subprocess, "DETACHED_PROCESS", 0x8)
    original_init = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if len(args) <= 13:  # creationflags is not passed positionally
            flags = int(kwargs.get("creationflags", 0) or 0)
            if not flags & (own_console | no_window):
                kwargs["creationflags"] = flags | no_window
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]
    subprocess.Popen._abstractgateway_no_window = True  # type: ignore[attr-defined]
    return True
