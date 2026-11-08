"""Keep the Mac awake while the gateway runs — a user setting (backlog 1149, operator 2026-11-08).

`abstractgateway serve` holds the macOS `PreventUserIdleSystemSleep` assertion (the one
`caffeinate -i` holds), named "AbstractGateway", while the setting `host.keep_awake` is on
(default on, so existing installs behave as before). Closing the lid or choosing Sleep still
sleeps the Mac, and the display still turns off. The assertion belongs to the gateway process:
macOS drops it if the process dies. Nothing is installed and no power setting is changed.

The setting is read at serve start (`runtime_config.resolve_keep_awake`: `serve --keep-awake
on|off` > stored > on) and changed LIVE through `PUT /api/gateway/host/keep-awake`: `apply(False)`
releases the assertion at once, `apply(True)` holds it again — no restart. `status(data_dir)` is the
one truth every door shows: `{value, source, held, platform_supported}`.

Other systems have no such assertion (root backlog 1101): `platform_supported` is false, nothing is
held, and the setting still stores. The independent check on a Mac: `pmset -g assertions` lists
`AbstractGateway` under PreventUserIdleSystemSleep while held, and not otherwise.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

ASSERTION_TYPE = "PreventUserIdleSystemSleep"
ASSERTION_NAME = "AbstractGateway"

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UTF8 = 0x08000100  # kCFStringEncodingUTF8
_LEVEL_ON = 255  # kIOPMAssertionLevelOn

# The one assertion this process may hold (its id), under a lock: the route
# thread and the serve thread both call `apply`.
_lock = threading.Lock()
_assertion_id: Optional[int] = None


def _say(message: str) -> None:
    print(f"[INFO] keep awake: {message}", file=sys.stderr, flush=True)


def _platform() -> str:
    """The platform the decision is made for (a function, so tests can name one)."""
    return sys.platform


def platform_supported(platform: Optional[str] = None) -> bool:
    return (platform or _platform()) == "darwin"


def _libraries():
    iokit = ctypes.cdll.LoadLibrary(_IOKIT)
    cf = ctypes.cdll.LoadLibrary(_CF)
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32
    iokit.IOPMAssertionCreateWithName.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    iokit.IOPMAssertionRelease.restype = ctypes.c_int32
    iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
    return iokit, cf


def hold(platform: Optional[str] = None) -> Optional[int]:
    """Take the assertion; its id, or None off macOS or when macOS refuses (said once on stderr)."""
    platform = platform or _platform()
    if platform != "darwin":
        _say(f"not available on {platform}; this computer may sleep while the gateway runs")
        return None
    try:
        iokit, cf = _libraries()
        kind = cf.CFStringCreateWithCString(None, ASSERTION_TYPE.encode(), _UTF8)
        name = cf.CFStringCreateWithCString(None, ASSERTION_NAME.encode(), _UTF8)
        try:
            if not kind or not name:
                raise RuntimeError("CoreFoundation could not build the assertion's strings")
            out = ctypes.c_uint32(0)
            rc = iokit.IOPMAssertionCreateWithName(kind, _LEVEL_ON, name, ctypes.byref(out))
        finally:
            for ref in (kind, name):
                if ref:  # CFRelease(NULL) aborts the process
                    cf.CFRelease(ref)
    except Exception as exc:  # noqa: BLE001 - never a reason not to serve
        _say(f"could not keep this Mac awake ({type(exc).__name__}: {exc})")
        return None
    if rc != 0:
        _say(f"macOS refused to keep this Mac awake (IOReturn {rc & 0xFFFFFFFF:#010x})")
        return None
    _say("this Mac does not idle-sleep while the gateway runs (closing the lid or choosing Sleep still sleeps it)")
    return int(out.value)


def release(assertion_id: Optional[int]) -> None:
    """Give it back (a no-op for None). macOS also drops it when the process exits."""
    if assertion_id is None:
        return
    try:
        iokit, _cf = _libraries()
        iokit.IOPMAssertionRelease(ctypes.c_uint32(int(assertion_id)))
    except Exception:  # noqa: BLE001 - the process exit drops it anyway
        pass


def held() -> bool:
    """True while this process holds the assertion."""
    with _lock:
        return _assertion_id is not None


def apply(enabled: bool, *, platform: Optional[str] = None) -> bool:
    """Make the live state match the setting: on → hold (once), off → release.
    Idempotent; returns whether the assertion is held afterwards. Off macOS
    nothing is ever held."""
    global _assertion_id
    with _lock:
        if enabled:
            if _assertion_id is None:
                # `hold` itself is the off-macOS no-op (it says so once); called
                # with no argument when none is named, as `serve` always did.
                _assertion_id = hold() if platform is None else hold(platform)
        elif _assertion_id is not None:
            release(_assertion_id)
            _assertion_id = None
            _say("released: this Mac may idle-sleep while the gateway runs")
        return _assertion_id is not None


def status(data_dir: Path, *, platform: Optional[str] = None) -> Dict[str, Any]:
    """`{value, source, held, platform_supported[, stored_value]}` — the setting
    (runtime_config: flag > stored > on) beside the live fact of this process."""
    from .runtime_config import resolve_keep_awake

    out = dict(resolve_keep_awake(Path(data_dir)))
    out["held"] = held()
    out["platform_supported"] = platform_supported(platform)
    return out
