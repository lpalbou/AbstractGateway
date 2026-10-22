"""An entity's appearance in client apps: one colour and one shape for the orb that stands for it
(the Apple Watch draws its responder's orb with them; operator request 2026-10-17).

Stored as `appearance.json` in the entity's home, next to its voice and substrate choices, and
changed like them by an admin or the entity's creator (`PUT /entities/{name}/appearance`). It is
presentation only: nothing in the entity's mind reads it, so unlike the voice and the substrate a
change writes no durable marker (the request's audit line records who changed it). Unset (no file)
means the client's own default look; each field of the file is read on its own, so a bad colour
never hides a good shape. A PUT replaces the whole appearance (no merge, so no lock is needed).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

FILENAME = "appearance.json"
SHAPES = ("planet", "kitty", "face", "wave")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_appearance(color: Optional[str], shape: Optional[str]) -> Dict[str, str]:
    """`{color, shape}` with what was given, validated: color `#RRGGBB` (stored lowercase), shape
    one of SHAPES. Raises ValueError naming the bad field; at least one field is required."""
    out: Dict[str, str] = {}
    c = str(color or "").strip()
    if c:
        if not _COLOR.match(c):
            raise ValueError(f"color must be #RRGGBB (six hex digits), not {c!r}")
        out["color"] = c.lower()
    s = str(shape or "").strip().lower()
    if s:
        if s not in SHAPES:
            raise ValueError(f"shape must be one of {', '.join(SHAPES)}, not {s!r}")
        out["shape"] = s
    if not out:
        raise ValueError("an appearance needs a color, a shape, or both")
    return out


def read_entity_appearance(home_dir: Any) -> Dict[str, str]:
    """The stored appearance (`{color?, shape?}`, each field kept only if valid), or {}."""
    try:
        data = json.loads((Path(home_dir) / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for key in ("color", "shape"):
        try:
            out.update(normalize_appearance(**{"color": None, "shape": None, key: data.get(key)}))
        except ValueError:
            pass
    return out


def write_entity_appearance(home_dir: Any, appearance: Dict[str, str]) -> None:
    """Writes a normalized appearance atomically (temp file + rename)."""
    clean = normalize_appearance(appearance.get("color"), appearance.get("shape"))
    home = Path(home_dir)
    fd, tmp = tempfile.mkstemp(prefix=".appearance-", suffix=".tmp", dir=str(home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(clean, indent=2) + "\n")
        os.replace(tmp, home / FILENAME)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def clear_entity_appearance(home_dir: Any) -> None:
    try:
        (Path(home_dir) / FILENAME).unlink()
    except FileNotFoundError:
        pass
