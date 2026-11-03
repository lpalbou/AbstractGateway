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
change (the settings door, the lock routes). They are called directly: a runtime without them
(older than 0.18) raises, and the failure is logged at ERROR with the version that has them; a
refusal in the runtime's answer (`ok: false`, a kept model that could not load) is logged at ERROR
too. Nothing is silently ignored.
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


def _log_refusals(name: str, answer: Any, why: str) -> None:
    """The runtime's answer, read as its contract says: `None` = recorded by the model host for the
    child's (re)build (applied and checked there); otherwise a dict whose `ok: false` is a refusal,
    and, for the locks, one `results` row per model with its own `ok`."""
    if answer is None:
        return
    if not isinstance(answer, dict):
        raise TypeError(f"{name} answered {type(answer).__name__}, not a dict")
    if answer.get("ok") is False and not answer.get("results"):
        logger.error("model policy: %s refused by the runtime (%s): %s", name, why, answer.get("error") or answer)
    for row in answer.get("results") or []:
        if isinstance(row, dict) and row.get("ok") is False:
            logger.error("model policy: %s/%s could not be kept loaded (%s, %s): %s", row.get("provider"),
                         row.get("model"), row.get("action"), why, row.get("error"))


def apply_model_policy(data_dir: Path, facade: Any, *, why: str) -> Dict[str, Any]:
    """Send the idle-unload seconds and the kept-loaded locks to the runtime host facade
    (AbstractRuntime >= 0.18: `set_model_idle_unload(seconds)`, `set_model_residency_locks(locks)`).
    Called directly: a facade without them raises (AttributeError), never skipped. Returns what was
    sent and the runtime's answers."""
    minutes = int(resolve_model_idle_unload(Path(data_dir))["value"])
    seconds = minutes * 60
    locks = kept_models(Path(data_dir))
    with _APPLY_LOCK:
        idle_answer = facade.set_model_idle_unload(seconds)
        _log_refusals(IDLE_SETTER, idle_answer, why)
        locks_answer = facade.set_model_residency_locks([{"provider": t["provider"], "model": t["model"]} for t in locks])
        _log_refusals(LOCKS_SETTER, locks_answer, why)
    logger.info("model policy applied after %s: idle unload %s, %d model(s) kept loaded", why,
                f"{minutes} min" if minutes else "off", len(locks))
    return {"idle_unload_s": seconds, "locks": locks, "answers": {IDLE_SETTER: idle_answer, LOCKS_SETTER: locks_answer}}


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
        try:
            apply_model_policy(Path(data_dir) if data_dir is not None else gateway_data_dir_from_env(), facade, why=why)
        except Exception:  # noqa: BLE001 - a daemon thread: the failure is logged loudly, with the runtime it needs
            logger.exception("model policy: not applied after %s (idle unload and Keep loaded need %s)", why, RUNTIME_NEEDED)

    thread = threading.Thread(target=_run, name="model-policy-apply", daemon=True)
    thread.start()
    return thread
