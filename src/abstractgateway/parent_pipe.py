"""Wait for the parent process to close the pipe it gave a child as stdin.

The gateway keeps the stdin of its children (model host, tray) open for their whole life: the
pipe closing means the gateway is gone or is stopping the child, on every OS and however the
gateway died.

POSIX reads until EOF. Windows must not: its C runtime keeps a descriptor locked for the whole of
a read, so with a thread parked in `stdin.read()` every other C runtime call on descriptor 0
waits forever, and extension modules make such calls when they load. `import numpy` never
returned in the model host (2026-11-17: the gateway's status probes piled up behind it until
every request was refused with 429). There the pipe is polled with PeekNamedPipe, which takes no
lock and never stays pending. A stdin that is not a pipe (a child started by hand in a terminal)
is read as on POSIX.
"""

from __future__ import annotations

import os
import time
from typing import BinaryIO

POLL_S = 0.2


def wait_closed(stream: BinaryIO) -> None:
    """Block until the other end of the pipe behind `stream` is closed. Never raises."""
    try:
        if os.name == "nt":
            import _winapi
            import msvcrt

            handle = msvcrt.get_osfhandle(stream.fileno())
            while _winapi.GetFileType(handle) == _winapi.FILE_TYPE_PIPE:
                waiting = _winapi.PeekNamedPipe(handle, 0)[0]  # OSError once the other end is closed
                if waiting:  # unread bytes would hide the close: dropped, without the C runtime
                    _winapi.ReadFile(handle, waiting)
                time.sleep(POLL_S)
        while stream.read(1024):
            pass
    except Exception:  # noqa: BLE001 - a broken or closed pipe is the answer
        return


__all__ = ["wait_closed"]
