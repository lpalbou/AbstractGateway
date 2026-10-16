"""Native confirm / info dialogs for the tray — no toolkit of our own.

- macOS: `NSAlert` in-process on the main thread (pyobjc is present because
  pystray requires it there). Menu callbacks already run on the main thread;
  a call from a worker thread is marshalled with `AppHelper.callAfter` and
  waits for the answer.
- Windows: `MessageBoxW` (Yes/No; Cancel-by-default for destructive asks).
- Linux: `zenity`, then `kdialog`. When neither exists `confirm()` returns
  None and the caller falls back to a two-step menu confirmation.

Copy rules: plain words, ≤ 3 short sentences,
Cancel is the default for destructive actions, and never the words runtime,
tick, residency, execution, process, PID, PyPI or RSS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable, Optional, Sequence

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"


# ---------------------------------------------------------------------------
# main-thread marshalling (macOS)
# ---------------------------------------------------------------------------


def run_on_main(fn: Callable[[], Any], *, timeout: Optional[float] = None) -> Any:
    """Call `fn` on the main thread and return its result (macOS only needs
    this; elsewhere it calls directly)."""
    if not IS_MAC or threading.current_thread() is threading.main_thread():
        return fn()
    from PyObjCTools import AppHelper  # type: ignore[import-not-found]

    done = threading.Event()
    box: dict = {}

    def _wrapped() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    AppHelper.callAfter(_wrapped)
    done.wait(timeout=timeout)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _mac_alert(title: str, body: str, buttons: list[str], *, style: str = "informational", default_index: int = 0, destructive_index: Optional[int] = None) -> int:
    import AppKit  # type: ignore[import-not-found]

    def _show() -> int:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(str(title))
        alert.setInformativeText_(str(body))
        styles = {
            "informational": getattr(AppKit, "NSAlertStyleInformational", 1),
            "warning": getattr(AppKit, "NSAlertStyleWarning", 0),
            "critical": getattr(AppKit, "NSAlertStyleCritical", 2),
        }
        alert.setAlertStyle_(styles.get(style, styles["informational"]))
        # NSAlert: the FIRST button added is the default (rightmost). We add
        # the default first, then the rest in order.
        order = [default_index] + [i for i in range(len(buttons)) if i != default_index]
        ns_buttons = {}
        for i in order:
            b = alert.addButtonWithTitle_(str(buttons[i]))
            ns_buttons[i] = b
            if destructive_index is not None and i == destructive_index and hasattr(b, "setHasDestructiveAction_"):
                try:
                    b.setHasDestructiveAction_(True)
                except Exception:
                    pass
        app = AppKit.NSApplication.sharedApplication()
        try:
            app.activateIgnoringOtherApps_(True)
        except Exception:
            pass
        resp = alert.runModal()
        first = getattr(AppKit, "NSAlertFirstButtonReturn", 1000)
        idx = int(resp) - int(first)
        return order[idx] if 0 <= idx < len(order) else -1

    return int(run_on_main(_show))


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def confirm(title: str, body: str, *, ok_label: str = "OK", cancel_label: str = "Cancel", danger: bool = False) -> Optional[bool]:
    """True = confirmed, False = cancelled, None = no dialog facility here."""
    try:
        if IS_MAC:
            buttons = [ok_label, cancel_label]
            idx = _mac_alert(
                title,
                body,
                buttons,
                style="warning" if danger else "informational",
                default_index=1 if danger else 0,
                destructive_index=0 if danger else None,
            )
            return idx == 0
        if IS_WIN:
            import ctypes

            MB_YESNO = 0x4
            MB_ICONQUESTION = 0x20
            MB_ICONWARNING = 0x30
            MB_DEFBUTTON2 = 0x100
            MB_TOPMOST = 0x40000
            MB_SETFOREGROUND = 0x10000
            flags = MB_YESNO | (MB_ICONWARNING if danger else MB_ICONQUESTION) | MB_TOPMOST | MB_SETFOREGROUND
            if danger:
                flags |= MB_DEFBUTTON2
            text = f"{body}\n\nYes = {ok_label}"
            res = ctypes.windll.user32.MessageBoxW(None, text, str(title), flags)  # type: ignore[attr-defined]
            return int(res) == 6  # IDYES
        zenity = shutil.which("zenity")
        if zenity:
            cmd = [zenity, "--question", "--title", str(title), "--text", f"<b>{_esc(title)}</b>\n\n{_esc(body)}", "--ok-label", ok_label, "--cancel-label", cancel_label, "--width", "380"]
            if danger:
                cmd.append("--default-cancel")
            p = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
            return p.returncode == 0
        kdialog = shutil.which("kdialog")
        if kdialog:
            cmd = [kdialog, "--title", str(title), "--yes-label", ok_label, "--no-label", cancel_label, "--warningyesno" if danger else "--yesno", body]
            p = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
            return p.returncode == 0
    except Exception:
        return None
    return None


def choose(title: str, body: str, buttons: Sequence[str], *, default_index: int = 0, cancel_index: Optional[int] = None, danger: bool = False) -> Optional[int]:
    """Ask with several buttons: the chosen button's index; `cancel_index` when
    the dialog is dismissed (Esc, the close box); None when no dialog
    facility exists here. Built for the approval dialog (Approve / Deny /
    Cancel): on Windows, zenity and kdialog it is exactly three buttons or
    nothing, and dismissing is always the third (never a Deny by accident)."""
    labels = [str(b) for b in buttons]
    if not labels:
        return None
    try:
        if IS_MAC:
            idx = _mac_alert(title, body, labels, style="warning" if danger else "informational", default_index=max(0, min(default_index, len(labels) - 1)))
            return idx if 0 <= idx < len(labels) else cancel_index
        if len(labels) != 3:
            return None
        if IS_WIN:
            import ctypes

            MB_YESNOCANCEL = 0x3
            MB_ICONQUESTION = 0x20
            MB_ICONWARNING = 0x30
            MB_DEFBUTTON2 = 0x100
            MB_DEFBUTTON3 = 0x200
            MB_TOPMOST = 0x40000
            MB_SETFOREGROUND = 0x10000
            flags = MB_YESNOCANCEL | (MB_ICONWARNING if danger else MB_ICONQUESTION) | MB_TOPMOST | MB_SETFOREGROUND
            flags |= {1: MB_DEFBUTTON2, 2: MB_DEFBUTTON3}.get(default_index, 0)
            text = f"{body}\n\nYes = {labels[0]} · No = {labels[1]} · Cancel = {labels[2]}"
            res = int(ctypes.windll.user32.MessageBoxW(None, text, str(title), flags))  # type: ignore[attr-defined]
            return {6: 0, 7: 1, 2: 2}.get(res, cancel_index)  # IDYES, IDNO, IDCANCEL
        zenity = shutil.which("zenity")
        if zenity:
            # The CANCEL label is the dismiss path (Esc); the second choice is the extra button.
            cmd = [zenity, "--question", "--title", str(title), "--text", f"<b>{_esc(title)}</b>\n\n{_esc(body)}", "--ok-label", labels[0], "--cancel-label", labels[2], "--extra-button", labels[1], "--width", "440"]
            if default_index != 0:
                cmd.append("--default-cancel")
            p = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
            if p.returncode == 0:
                return 0
            return 1 if p.stdout.decode("utf-8", "replace").strip() == labels[1] else cancel_index
        kdialog = shutil.which("kdialog")
        if kdialog:
            cmd = [kdialog, "--title", str(title), "--yes-label", labels[0], "--no-label", labels[1], "--cancel-label", labels[2], "--warningyesnocancel" if danger else "--yesnocancel", body]
            p = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
            return {0: 0, 1: 1}.get(p.returncode, cancel_index)
    except Exception:
        return None
    return None


def info(title: str, body: str, *, style: str = "informational") -> bool:
    """Show an informational alert; False when no dialog facility exists."""
    try:
        if IS_MAC:
            _mac_alert(title, body, ["OK"], style=style)
            return True
        if IS_WIN:
            import ctypes

            MB_OK = 0x0
            MB_ICONINFORMATION = 0x40
            MB_ICONWARNING = 0x30
            MB_TOPMOST = 0x40000
            MB_SETFOREGROUND = 0x10000
            icon = MB_ICONWARNING if style in {"warning", "critical"} else MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, str(body), str(title), MB_OK | icon | MB_TOPMOST | MB_SETFOREGROUND)  # type: ignore[attr-defined]
            return True
        zenity = shutil.which("zenity")
        if zenity:
            kind = "--warning" if style in {"warning", "critical"} else "--info"
            subprocess.run([zenity, kind, "--title", str(title), "--text", f"<b>{_esc(title)}</b>\n\n{_esc(body)}", "--width", "380"], capture_output=True, timeout=600, check=False)
            return True
        kdialog = shutil.which("kdialog")
        if kdialog:
            kind = "--sorry" if style in {"warning", "critical"} else "--msgbox"
            subprocess.run([kdialog, "--title", str(title), kind, body], capture_output=True, timeout=600, check=False)
            return True
    except Exception:
        return False
    return False


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def set_mac_app_identity(name: str = "AbstractGateway") -> None:
    """Best-effort: name the bare-python process so alerts and the menu bar
    say AbstractGateway, and keep it out of the Dock (accessory policy)."""
    if not IS_MAC:
        return
    try:
        import AppKit  # type: ignore[import-not-found]
        import Foundation  # type: ignore[import-not-found]

        try:
            bundle = Foundation.NSBundle.mainBundle()
            info_dict = bundle.localizedInfoDictionary() or bundle.infoDictionary()
            if info_dict is not None:
                info_dict["CFBundleName"] = name
                info_dict["CFBundleDisplayName"] = name
        except Exception:
            pass
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(getattr(AppKit, "NSApplicationActivationPolicyAccessory", 1))
    except Exception:
        pass
