"""Small OS helpers for the tray: dark-mode detection, opening URLs, parent
liveness. Every function is best-effort and cheap; none imports a GUI kit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from typing import Optional

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")


def _run(cmd: list[str], *, timeout: float = 2.0) -> Optional[str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return (p.stdout or "").strip()


def system_prefers_dark() -> Optional[bool]:
    """Whether the OS chrome (menu bar / taskbar / panel) is dark; None when
    the platform gives no confident answer (the icon then uses its neutral
    palette, which is legible on either bar)."""
    try:
        if IS_MAC:
            # `defaults` errors out when the key is absent = Light mode.
            out = _run(["defaults", "read", "-g", "AppleInterfaceStyle"])
            return bool(out and "dark" in out.lower())
        if IS_WIN:
            try:
                import winreg  # type: ignore[import-not-found]

                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                    # The TASKBAR/tray follows SystemUsesLightTheme, not AppsUseLightTheme.
                    value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
                    return int(value) == 0
            except Exception:
                return None
        if IS_LINUX:
            out = _run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"])
            if out and "prefer-dark" in out.lower():
                return True
            if out and "prefer-light" in out.lower():
                return False
            return None
    except Exception:
        return None
    return None


def open_url(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WIN:
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))  # type: ignore[attr-defined]
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))  # type: ignore[attr-defined]
                STILL_ACTIVE = 259
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        except Exception:
            return True  # cannot tell: assume alive rather than quit on the user
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def parent_process_start_time(pid: int) -> Optional[float]:
    """Best-effort start time of a pid (to detect PID reuse on Linux/macOS)."""
    try:
        if IS_LINUX:
            with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
                return float(fields[19])  # starttime in clock ticks since boot
        if IS_MAC:
            out = _run(["ps", "-o", "lstart=", "-p", str(int(pid))])
            return float(hash(out)) if out else None
    except Exception:
        return None
    return None


def copy_to_clipboard(text: str) -> bool:
    try:
        if IS_MAC:
            p = subprocess.run(["pbcopy"], input=text, text=True, timeout=2, check=False)
            return p.returncode == 0
        if IS_WIN:
            p = subprocess.run(["clip"], input=text, text=True, timeout=2, check=False)
            return p.returncode == 0
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            try:
                p = subprocess.run(cmd, input=text, text=True, timeout=2, check=False)
                if p.returncode == 0:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def monotonic() -> float:
    return time.monotonic()
