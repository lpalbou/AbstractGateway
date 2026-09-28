"""The local gateway pointer: `~/.abstractframework/gateway.json` (root backlog 0943).

Where THIS computer's installed gateway listens, for clients that cannot call
`first_run.local_gateway()` (the Rust TUIs, the Node apps, the frozen
Assistant). One small file, no token, no pid, no liveness:

    {"schema": 1, "url": "http://127.0.0.1:8081", "port": 8081,
     "data_dir": "/home/me/.local/share/abstractgateway",
     "updated_at": "2026-09-27T12:00:00Z", "written_by": "installer|serve"}

Writers: the installer (always: it owns the install) and `abstractgateway
serve`, once its listener is bound, with the port it bound. Serve writes only
under the OWNERSHIP RULE, so a test or second gateway never takes the file
over:
- the file is absent (or unreadable) and serve's data dir is the OS default
  data dir (`host_paths.user_data_dir()`); or
- the file's `data_dir` is serve's own data dir.
Paths are compared resolved, never by where the data dir came from (the
service unit always exports ABSTRACTGATEWAY_DATA_DIR, even for the default).

`network set` never writes it: clients move when the gateway does (at the
restart that binds the new port). Serve never deletes it: the port stays
valid while the gateway is stopped. The installer's uninstall deletes it.

Readers (other languages) believe it only when `schema` is 1, the url is on
127.0.0.1 / ::1 / localhost, and (POSIX) the file is owned by the reader.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

POINTER_SCHEMA = 1
_LOOPBACK_HOSTS = ("127.0.0.1", "[::1]", "localhost")


def gateway_pointer_path(home: Optional[Path] = None) -> Path:
    """`~/.abstractframework/gateway.json` on every OS."""
    return Path(home if home is not None else Path.home()) / ".abstractframework" / "gateway.json"


def read_gateway_pointer(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The pointer as stored (a dict), or None when absent or unreadable."""
    p = path or gateway_pointer_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _same_dir(a: Any, b: Any) -> bool:
    try:
        return Path(str(a)).expanduser().resolve() == Path(str(b)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError):
        return False


def serve_owns_pointer(data_dir: Path, *, existing: Optional[Dict[str, Any]], default_data_dir: Path) -> Tuple[bool, str]:
    """(may serve write, why) by the ownership rule (module docstring)."""
    if existing is None:
        if _same_dir(data_dir, default_data_dir):
            return True, "no pointer yet and this gateway uses the default data directory"
        return False, f"no pointer yet and this gateway's data directory is not the default ({default_data_dir})"
    if _same_dir(existing.get("data_dir"), data_dir):
        return True, "the pointer names this gateway's data directory"
    return False, f"the pointer belongs to the gateway with data directory {existing.get('data_dir')!r}"


def write_gateway_pointer(*, url: str, port: int, data_dir: Path, written_by: str, path: Optional[Path] = None) -> Path:
    """Write the pointer atomically (fresh temp file + replace), mode 0600;
    a symlink at the target is replaced, never followed."""
    p = path or gateway_pointer_path()
    host = str(url).split("://", 1)[-1].rsplit(":", 1)[0]
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(f"the gateway pointer names a loopback URL only (got {url!r})")
    payload = {
        "schema": POINTER_SCHEMA,
        "url": str(url).rstrip("/"),
        "port": int(port),
        "data_dir": str(Path(data_dir).expanduser().resolve()),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "written_by": str(written_by),
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    # The temp file is created fresh (mkstemp: random name, O_EXCL, mode
    # 0600), so a symlink planted at a predictable temp name cannot redirect
    # the write. os.replace renames onto the target: a symlink at the target
    # is REPLACED by the regular file, never followed.
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return p


def record_serve_pointer(*, host: str, port: int, data_dir: Path, path: Optional[Path] = None, default_data_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """`serve`, once bound: write the pointer when the ownership rule allows.
    Returns (written, one plain sentence for the log)."""
    from .first_run import browser_base_url
    from .host_paths import user_data_dir

    p = path or gateway_pointer_path()
    url = browser_base_url(host, int(port))
    if url.split("://", 1)[-1].rsplit(":", 1)[0] not in _LOOPBACK_HOSTS:
        return False, f"gateway pointer not written: this gateway listens on {host} only, not on this computer's loopback"
    owns, why = serve_owns_pointer(Path(data_dir), existing=read_gateway_pointer(p), default_data_dir=default_data_dir or user_data_dir())
    if not owns:
        return False, f"gateway pointer {p} left unchanged: {why}"
    write_gateway_pointer(url=url, port=int(port), data_dir=Path(data_dir), written_by="serve", path=p)
    return True, f"gateway pointer {p} -> {url} ({why})"


def pointer_status(data_dir: Path, *, running_port: Optional[int], path: Optional[Path] = None) -> Dict[str, Any]:
    """For `abstractgateway network status`: the pointer's URL, whether it
    names this data dir, and whether it matches the port this gateway runs
    on. {path, present, url, port, written_by, this_data_dir, matches_running}."""
    p = path or gateway_pointer_path()
    data = read_gateway_pointer(p)
    if data is None:
        return {"path": str(p), "present": p.exists(), "readable": False, "url": None, "port": None, "written_by": None, "this_data_dir": False, "matches_running": None}
    port = data.get("port")
    return {
        "path": str(p),
        "present": True,
        "readable": True,
        "url": data.get("url"),
        "port": port,
        "written_by": data.get("written_by"),
        "this_data_dir": _same_dir(data.get("data_dir"), data_dir),
        "matches_running": None if running_port is None else (port == int(running_port)),
    }
