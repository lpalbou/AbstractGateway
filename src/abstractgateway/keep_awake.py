"""While the gateway runs, the Mac does not idle-sleep (operator ruling 2026-11-02).

`abstractgateway serve` holds the macOS `PreventUserIdleSystemSleep` assertion (the one
`caffeinate -i` holds), named "AbstractGateway", from the moment it serves until it stops.
Closing the lid or choosing Sleep still sleeps the Mac, and the display still turns off.
The assertion belongs to the gateway process: macOS drops it if the process dies. Nothing
is installed and no power setting is changed.

Other systems: nothing yet (root backlog 1101).
"""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

ASSERTION_TYPE = "PreventUserIdleSystemSleep"
ASSERTION_NAME = "AbstractGateway"

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UTF8 = 0x08000100  # kCFStringEncodingUTF8
_LEVEL_ON = 255  # kIOPMAssertionLevelOn


def _say(message: str) -> None:
    print(f"[INFO] keep awake: {message}", file=sys.stderr, flush=True)


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


def hold(platform: str = sys.platform) -> Optional[int]:
    """Take the assertion; its id, or None off macOS or when macOS refuses (said once on stderr)."""
    if platform != "darwin":
        _say(f"not available on {platform}; this computer may sleep while the gateway runs")
        return None
    try:
        iokit, cf = _libraries()
        kind = cf.CFStringCreateWithCString(None, ASSERTION_TYPE.encode(), _UTF8)
        name = cf.CFStringCreateWithCString(None, ASSERTION_NAME.encode(), _UTF8)
        try:
            out = ctypes.c_uint32(0)
            rc = iokit.IOPMAssertionCreateWithName(kind, _LEVEL_ON, name, ctypes.byref(out))
        finally:
            cf.CFRelease(kind)
            cf.CFRelease(name)
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
