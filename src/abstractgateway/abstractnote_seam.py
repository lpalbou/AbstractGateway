"""The optional AbstractNote integration (the `abstractnote` Python package, when installed).

AbstractGateway has no plugin loader; this module is the one explicit seam (AbstractNote ADR
0001). With `abstractnote` importable the gateway:

- binds the note tools to each plane's own notebook (`<plane data dir>/abstractnote/`) next to
  `read_skill` (hosts/bundle_host.py), with the host's artifact store as the only attachment
  source (no file paths) and the plane's account stamped on captures (`note_tool_map`);
- adds the note tool names to the plane's default tool-approval auto-approve set
  (`note_auto_approve_names`): they read and write the caller's own notebook only, and a capture
  must never park on an approval wait;
- serves the note read API under `/api/gateway/note` (`note_router`, mounted by app.py);
- loads the `abstractnote` workflows into every plane's private registry as a framework bundle
  (`note_bundle_paths`), packed from the installed package into
  `<root data dir>/extensions/abstractnote/<content hash>/` and read by the same bundle loader
  as the gateway's own shipped bundles.

Without the package every function answers "nothing" and the gateway behaves exactly as before.
No function here raises: a package that is installed but broken is logged once and treated as
absent, a binding failure answers on every tool call, a packing failure leaves the workflows out.
The import is attempted once and its outcome logged once.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: Dict[str, Any] = {}


def abstractnote_available() -> bool:
    """True when `abstractnote.gateway` imports; the result (and its reason) is logged once."""
    with _lock:
        if "available" not in _state:
            try:
                import abstractnote.gateway  # noqa: F401

                _state["available"] = True
                logger.info("AbstractNote is installed: note tools, /api/gateway/note and its workflows are enabled")
            except ImportError as e:
                _state["available"] = False
                logger.info("AbstractNote is not installed (%s): the note integration is off", e)
            except Exception as e:  # noqa: BLE001 - a broken package must not take the gateway down
                _state["available"] = False
                logger.error("AbstractNote is installed but cannot be loaded (%s): the note integration is off", e)
        return bool(_state["available"])


def note_auto_approve_names() -> FrozenSet[str]:
    """The note tool names to auto-approve (empty without abstractnote; never raises, so the
    approval executor is always built with its default policy at least)."""
    if not abstractnote_available():
        return frozenset()
    try:
        from abstractnote.gateway.tools import AUTO_APPROVE_TOOL_NAMES

        return frozenset(str(n) for n in AUTO_APPROVE_TOOL_NAMES)
    except Exception as e:  # noqa: BLE001
        logger.error("AbstractNote tool names could not be read: %s", e)
        return frozenset()


def _failing_tool(name: str, message: str) -> Callable[..., Any]:
    def tool(**_kwargs: Any) -> Any:
        raise ValueError(message)

    tool.__name__ = name
    return tool


def note_tool_map(*, data_root: Path, artifact_store: Any, account: Optional[str]) -> Dict[str, Callable[..., Any]]:
    """The plane's note tools ({} without abstractnote).

    A binding failure (for example a notebook pinned to another embedding model) must not take
    the plane's runtime down: it is logged as an error and every note tool then answers with
    that sentence, so a capture fails loudly instead of the whole gateway.
    """
    if not abstractnote_available():
        return {}
    try:
        from abstractnote.gateway.tools import bound_tool_map

        from .memory_store import build_gateway_memory_embedder

        embedder = build_gateway_memory_embedder(base_dir=Path(data_root))
        return bound_tool_map(Path(data_root), embedder, artifact_store, account)
    except Exception as e:  # noqa: BLE001 - surfaced on every call and in the log
        logger.error("AbstractNote tools could not be bound for %s: %s", data_root, e)
        message = f"Your notebook is not available: {e}"
        return {name: _failing_tool(name, message) for name in note_auto_approve_names()}


def note_router() -> Optional[Any]:
    """The note read API router (prefix `/note`), or None without abstractnote."""
    if not abstractnote_available():
        return None
    try:
        from abstractnote.gateway.router import router

        return router
    except Exception as e:  # noqa: BLE001 - the gateway boots without the note API, loudly
        logger.error("AbstractNote's read API could not be loaded: %s", e)
        return None


def note_bundle_paths(root_data_dir: Path) -> List[Path]:
    """The packed `abstractnote@<version>.flow` for this install ([] without abstractnote).

    Packed once per content: the directory name is a hash of the generated flow documents and
    the package version, so an upgrade brings a fresh bundle and a restart reuses the file.
    """
    if not abstractnote_available():
        return []
    try:
        from abstractnote.flows.build import ALL_FLOWS, JSON_DIR
        from abstractnote.flows.bundle import bundle_version, pack

        digest = hashlib.sha256(bundle_version().encode("utf-8"))
        for fid in ALL_FLOWS:
            digest.update((JSON_DIR / f"{fid}.json").read_bytes())
        out_dir = Path(root_data_dir) / "extensions" / "abstractnote" / digest.hexdigest()[:16]
        target = out_dir / f"abstractnote@{bundle_version()}.flow"
        with _lock:
            if not target.is_file():
                # Packed beside its final place and moved in whole, so a concurrent plane
                # build never opens a half-written bundle.
                tmp_dir = out_dir.with_name(out_dir.name + ".tmp")
                shutil.rmtree(tmp_dir, ignore_errors=True)
                tmp_dir.mkdir(parents=True, exist_ok=True)
                packed = pack(tmp_dir, flows_dir=JSON_DIR)
                out_dir.mkdir(parents=True, exist_ok=True)
                packed.replace(target)
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return [target]
    except Exception as e:  # noqa: BLE001 - a broken package must not block the boot; said loudly
        logger.error("AbstractNote workflows could not be packed: %s", e)
        return []


__all__ = [
    "abstractnote_available",
    "note_auto_approve_names",
    "note_bundle_paths",
    "note_router",
    "note_tool_map",
]
