"""AbstractGateway desktop tray helper (system tray / menu bar icon).

Launched by `abstractgateway serve` as a child process (see
`abstractgateway.tray_supervisor`); talks to the gateway over loopback HTTP
only. Import-safe without pystray/Pillow/tkinter — the GUI modules import
them lazily so `python -m abstractgateway.tray --help` and the unit tests
work on a headless box.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from .__main__ import main as _main

    return _main(argv)
