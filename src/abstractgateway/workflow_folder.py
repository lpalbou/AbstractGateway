"""The workflow folder a data folder remembers (2026-11-28).

A workflow that exists must be used, whichever code runs: an automation pins `bundle@version:flow`
in the DATA folder, so the folder holding it cannot depend on where the running code sits
(2026-11-07: gateways run from other copies of the code lost the published versions). The service
remembers the folder it uses in `<data dir>/config/workflow-folder.json`; `GatewayHostConfig.from_env`
picks: `chosen` (the process's `--flows-dir`), else `serving` (the folder this process already serves
for that data folder), else this remembered folder, else the folder next to the running code.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional


#: The `--flows-dir` of this process (`serve` / `runner`); also how in-process callers and tests name
#: the folder. None: the remembered folder, else the folder next to the running code.
chosen: Optional[Path] = None

#: resolved root data dir -> the folder this process's service build serves for it (`started`): a
#: process keeps that folder, whatever another process records later. One process can serve several
#: data dirs (tests do), hence per data dir.
serving: Dict[Path, Path] = {}


def record_path(data_dir: Path) -> Path:
    return Path(data_dir) / "config" / "workflow-folder.json"


def _recorded(data_dir: Path) -> Optional[str]:
    """The record's `folder` text; None when it cannot be read or holds none."""
    try:
        folder = json.loads(record_path(data_dir).read_text(encoding="utf-8")).get("folder")
    except (OSError, ValueError, AttributeError):
        return None
    return folder if isinstance(folder, str) else None


def remembered(data_dir: Path) -> Optional[Path]:
    """The folder this data folder remembers; None when there is no record, it cannot be read, or
    it names something that is not an absolute path to a directory."""
    folder = _recorded(data_dir)
    path = Path(folder) if folder else None
    return path if path is not None and path.is_absolute() and path.is_dir() else None


def started(data_dir: Path, folder: Path) -> None:
    """A service build for `data_dir` serves `folder`: this process keeps it (`serving`), and the
    data folder remembers it when this process named the folder (`chosen`) or nothing is recorded
    yet. A record whose folder is missing for this start (or that cannot be read) is left as it is,
    so the folder is served again once it is back; the start says so in one line."""
    data_dir, folder = Path(data_dir).expanduser().resolve(), Path(folder).expanduser().resolve()
    first = data_dir not in serving
    serving[data_dir] = folder
    if chosen is not None or not record_path(data_dir).exists():
        remember(data_dir, folder)
    elif first and remembered(data_dir) is None:
        logging.getLogger(__name__).error(
            "the workflow folder remembered for %s (%s) is not available; serving %s for this start, the record is kept",
            data_dir, _recorded(data_dir) or f"{record_path(data_dir)} cannot be read", folder,
        )


def remember(data_dir: Path, folder: Path) -> None:
    """Remember `folder` (absolute), atomically; written only when the value differs."""
    folder = Path(folder).expanduser().resolve()
    path = record_path(data_dir)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("folder") == str(folder):
            return
    except (OSError, ValueError, AttributeError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"folder": str(folder)}) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:  # never blocks a start; said where the service's log shows it
        logging.getLogger(__name__).error("the workflow folder %s could not be remembered in %s: %s", folder, path, e)
