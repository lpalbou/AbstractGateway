"""The gateway's model policy for the runtime: idle unload and kept-loaded locks (root backlog 1115).

Operator rulings 2026-11-03:

- **Idle unload.** A loaded model no request has used for N minutes is unloaded; default 60, 0 =
  never; `serve --model-idle-unload MINUTES` > stored `models.idle_unload_minutes` > 60 (resolved
  in `runtime_config.resolve_model_idle_unload`). AbstractCore owns the reaper; the gateway hands
  the number to the runtime facade as seconds: `set_model_idle_unload(seconds)`.
- **Keep loaded persists.** A lock the runtime confirmed is recorded in the gateway's settings
  (`models.kept_loaded`, a list of `{provider, model[, base_url]}`); an unlock (whatever the
  runtime answers) or a forced unload removes it. The whole list goes to the runtime facade as
  `set_model_residency_locks(locks)`: a `set_*` call, so the model host replays it after a child
  restart, and the gateway sends it again after its own start.

Both setters are sent after the gateway starts (`apply_in_background`) and again after every
change (the settings door, the lock routes). A runtime without them is reported at ERROR with the
version that has them; nothing is silently ignored.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .runtime_config import MODEL_KEPT_LOADED_KEY, _read_store, _write_store, resolve_model_idle_unload, store_lock

logger = logging.getLogger("abstractgateway.kept_models")

RUNTIME_NEEDED = "AbstractRuntime >= 0.18"
IDLE_SETTER = "set_model_idle_unload"
LOCKS_SETTER = "set_model_residency_locks"
_APPLY_LOCK = threading.Lock()  # one apply at a time (boot and a settings change can overlap)


def _target(provider: Any, model: Any, base_url: Any = None) -> Optional[Dict[str, str]]:
    p = str(provider or "").strip()
    m = str(model or "").strip()
    if not p or not m:
        return None
    out = {"provider": p, "model": m}
    b = str(base_url or "").strip()
    if b:
        out["base_url"] = b
    return out


def _same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (str(a.get("provider") or "").lower(), str(a.get("model") or ""), str(a.get("base_url") or "")) == (
        str(b.get("provider") or "").lower(), str(b.get("model") or ""), str(b.get("base_url") or ""))


def kept_models(data_dir: Path) -> List[Dict[str, str]]:
    rows = _read_store(Path(data_dir)).get(MODEL_KEPT_LOADED_KEY)
    out: List[Dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            t = _target(row.get("provider"), row.get("model"), row.get("base_url"))
            if t is not None:
                out.append(t)
    return out


def remember_kept(data_dir: Path, provider: Any, model: Any, base_url: Any = None) -> bool:
    """Record one kept-loaded model; returns whether the store changed."""
    target = _target(provider, model, base_url)
    if target is None:
        return False
    with store_lock(Path(data_dir)):
        stored = _read_store(Path(data_dir), strict=True)
        rows = [r for r in (stored.get(MODEL_KEPT_LOADED_KEY) or []) if isinstance(r, dict)]
        if any(_same(r, target) for r in rows):
            return False
        stored[MODEL_KEPT_LOADED_KEY] = rows + [target]
        _write_store(Path(data_dir), stored)
    return True


def forget_kept(data_dir: Path, provider: Any, model: Any, base_url: Any = None) -> bool:
    """Remove a kept-loaded record (any base_url when none is given); returns whether it changed."""
    p = str(provider or "").strip().lower()
    m = str(model or "").strip()
    if not p or not m:
        return False
    b = str(base_url or "").strip()
    with store_lock(Path(data_dir)):
        stored = _read_store(Path(data_dir), strict=True)
        rows = [r for r in (stored.get(MODEL_KEPT_LOADED_KEY) or []) if isinstance(r, dict)]
        keep = [r for r in rows if not (str(r.get("provider") or "").lower() == p and str(r.get("model") or "") == m
                                        and (not b or str(r.get("base_url") or "") == b))]
        if len(keep) == len(rows):
            return False
        if keep:
            stored[MODEL_KEPT_LOADED_KEY] = keep
        else:
            stored.pop(MODEL_KEPT_LOADED_KEY, None)
        _write_store(Path(data_dir), stored)
    return True


def apply_model_policy(data_dir: Path, facade: Any, *, why: str) -> Dict[str, Any]:
    """Send the idle-unload seconds and the kept-loaded locks to the runtime facade. Returns what was
    sent per setter (`{"idle_unload_s": ..., "locks": [...], "missing": [...]}`); never raises."""
    minutes = int(resolve_model_idle_unload(Path(data_dir))["value"])
    seconds = minutes * 60
    locks = kept_models(Path(data_dir))
    report: Dict[str, Any] = {"idle_unload_s": seconds, "locks": locks, "missing": []}
    with _APPLY_LOCK:
        for name, value in ((IDLE_SETTER, seconds), (LOCKS_SETTER, locks)):
            fn = getattr(facade, name, None) if facade is not None else None
            if not callable(fn):
                report["missing"].append(name)
                logger.error("model policy: the runtime has no %s (needs %s); %s is not applied (%s)", name,
                             RUNTIME_NEEDED, "idle unload" if name == IDLE_SETTER else "Keep loaded across restarts", why)
                continue
            try:
                fn(value)
            except Exception as exc:  # noqa: BLE001 - logged with the setting it concerns
                report.setdefault("errors", {})[name] = str(exc)
                logger.error("model policy: %s refused by the runtime (%s): %s", name, why, exc)
    logger.info("model policy applied after %s: idle unload %s, %d model(s) kept loaded", why,
                f"{minutes} min" if minutes else "off", len(locks))
    return report


def apply_in_background(why: str, *, data_dir: Optional[Path] = None,
                        facade_getter: Optional[Callable[[], Any]] = None) -> threading.Thread:
    """`apply_model_policy` on a daemon thread, through the gateway's runtime host facade (the one the
    `/models/*` routes use)."""
    def _run() -> None:
        from .users import gateway_data_dir_from_env

        if facade_getter is not None:
            facade = facade_getter()
        else:
            from .routes.gateway import _gateway_abstractcore_host_facade

            facade, err = _gateway_abstractcore_host_facade()
            if err or facade is None:
                logger.error("model policy: no runtime host facade (%s); idle unload and Keep loaded are not applied", err)
                return
        apply_model_policy(Path(data_dir) if data_dir is not None else gateway_data_dir_from_env(), facade, why=why)

    thread = threading.Thread(target=_run, name="model-policy-apply", daemon=True)
    thread.start()
    return thread
