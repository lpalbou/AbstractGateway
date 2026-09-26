"""Where the gateway reads its skills from (setting `skills.shelf`, CONTRACTS §X).

A shelf is a registry folder holding `skills/<name>/SKILL.md` and the trust
files (`validations.yaml`, `advisories.yaml`, `guidance.yaml`). Resolution,
first that yields a folder:

1. `stored`  - the saved setting `skills.shelf` (console, console TUI,
               `abstractgateway config set skills.shelf PATH`);
2. `env`     - the legacy launch environment ABSTRACTGATEWAY_SKILLS_SHELF
               (reported, never the documented way);
3. `seeded`  - `<data dir>/skills/registry`, a copy of the curated registry
               that ships inside the abstractskill package, seeded at start
               (`seed_registry` adds what is missing and never overwrites an
               operator's edit);
4. `checkout`- only when the seeded copy is missing (seeding failed): a
               framework checkout's registry next to the backlog folder
               (`abstractskill/src/abstractskill/registry`, or the older
               `abstractskill/registry`) when that folder exists.

A saved or environment value that is not a registry folder does NOT fall
through to the next rung: the shelf is then unavailable and says why (an
operator who pointed the gateway somewhere must see that it is wrong, not
silently get another shelf).

The seed runs at gateway start (service.create_default_gateway_service, once
per process), never at import; abstractskill locks concurrent seeds of one
folder, so several gateway processes on one data dir are safe.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SETTING_KEY = "skills.shelf"
ENV_NAME = "ABSTRACTGATEWAY_SKILLS_SHELF"
SEEDED_RELPATH = ("skills", "registry")
LABEL = "Skills shelf"
HELP = (
    "The folder the gateway reads skills from (it holds skills/<name>/SKILL.md and the trust files). "
    "Empty: the gateway's own copy of the curated shelf that ships with AbstractSkill, kept up to date at each start."
)

_seed_lock = threading.Lock()
_seed_state: Dict[str, Any] = {}  # data_dir -> last seed outcome (this process)


def seeded_registry_dir(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve().joinpath(*SEEDED_RELPATH)


def _is_registry(path: Path) -> bool:
    return (path / "skills").is_dir()


def registry_problem(path: Path) -> Optional[str]:
    if not path.is_absolute():
        return f"{path} is not an absolute path"
    if not path.is_dir():
        return f"no folder at {path}"
    if not _is_registry(path):
        return f"{path} has no skills/ folder (a shelf holds skills/<name>/SKILL.md and the trust files)"
    return None


def validate_shelf_value(raw: Any) -> str:
    """The write-time check (every door): an absolute folder holding skills/."""
    from .runtime_config import RuntimeConfigError

    text = str(raw if raw is not None else "").strip()
    path = Path(text).expanduser()
    problem = registry_problem(path)
    if problem:
        raise RuntimeConfigError(f"{SETTING_KEY}: {problem}")
    return str(path.resolve())


def _bundled_api() -> Any:
    """abstractskill.bundled (abstractskill >= 0.3.0), or a RuntimeError that
    says the installed abstractskill is too old."""
    try:
        from abstractskill import bundled  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"the installed abstractskill has no bundled skill registry (abstractskill 0.3.0 or newer is required): {exc}"
        ) from exc
    for name in ("bundled_registry_dir", "bundled_registry_version", "seed_registry"):
        if not callable(getattr(bundled, name, None)):
            raise RuntimeError(f"abstractskill.bundled has no {name}(); abstractskill 0.3.0 or newer is required")
    return bundled


def bundled_version() -> Optional[str]:
    try:
        return str(_bundled_api().bundled_registry_version())
    except Exception:  # noqa: BLE001 - reported through the warnings of the caller
        return None


def seed_report_dict(report: Any) -> Dict[str, Any]:
    """A SeedReport as JSON (every bucket, whatever the library adds)."""
    out: Dict[str, Any] = {}
    fields = getattr(report, "__dataclass_fields__", None) or {}
    for name in fields:
        value = getattr(report, name, None)
        if isinstance(value, Path):
            value = str(value)
        elif isinstance(value, tuple):
            value = list(value)
        out[name] = value
    out["changed"] = bool(getattr(report, "changed", False))
    kept = getattr(report, "kept", None)
    if isinstance(kept, dict):
        out["kept"] = dict(kept)
    return out


def seed(data_dir: Path) -> Dict[str, Any]:
    """Seed `<data dir>/skills/registry` now; returns {ok, report | error}.
    The outcome is kept for /skills (this process)."""
    dest = seeded_registry_dir(data_dir)
    try:
        bundled = _bundled_api()
        dest.parent.mkdir(parents=True, exist_ok=True)
        report = seed_report_dict(bundled.seed_registry(dest))
        outcome: Dict[str, Any] = {"ok": True, "dest": str(dest), "report": report}
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to a start
        outcome = {"ok": False, "dest": str(dest), "error": f"{type(exc).__name__}: {exc}"}
    with _seed_lock:
        _seed_state[str(dest)] = outcome
    return outcome


def seed_at_start(data_dir: Path) -> Dict[str, Any]:
    """Seed once per process and data dir; the report is logged once."""
    dest = str(seeded_registry_dir(data_dir))
    with _seed_lock:
        done = _seed_state.get(dest)
    if done is not None:
        return done
    outcome = seed(data_dir)
    if outcome["ok"]:
        r = outcome["report"]
        kept = r.get("kept") or {}
        logger.info(
            "skills shelf seeded at %s (bundle %s, previously %s): %d added, %d updated, %d unchanged, %d kept%s",
            dest, r.get("bundled_version"), r.get("previous_version"), len(r.get("added") or []),
            len(r.get("updated") or []), len(r.get("unchanged") or []), len(kept),
            (" (" + ", ".join(f"{k}: {v}" for k, v in sorted(kept.items())) + ")") if kept else "",
        )
    else:
        logger.warning("skills shelf NOT seeded at %s: %s", dest, outcome["error"])
    return outcome


def last_seed(data_dir: Path) -> Optional[Dict[str, Any]]:
    with _seed_lock:
        return _seed_state.get(str(seeded_registry_dir(data_dir)))


def _checkout_candidates(checkout_root: Optional[Path]) -> List[Path]:
    if checkout_root is None:
        return []
    root = Path(checkout_root)
    return [root / "abstractskill" / "src" / "abstractskill" / "registry", root / "abstractskill" / "registry"]


def resolve_skills_shelf(
    data_dir: Path,
    *,
    checkout_root: Optional[Path] = None,
    env: Optional[Any] = None,
) -> Dict[str, Any]:
    """{registry: Path|None, source: stored|env|seeded|checkout|none,
    value (the configured text), reason (when None), warnings[]}."""
    from .runtime_config import _read_store

    env = os.environ if env is None else env
    warnings: List[str] = []
    skills_block = _read_store(Path(data_dir)).get("skills")
    stored = skills_block.get("shelf") if isinstance(skills_block, dict) else None
    if isinstance(stored, str) and stored.strip():
        path = Path(stored).expanduser()
        problem = registry_problem(path)
        if problem:
            return {"registry": None, "source": "stored", "value": stored, "reason": f"the saved {SETTING_KEY} is not usable: {problem}", "warnings": warnings}
        return {"registry": path.resolve(), "source": "stored", "value": stored, "reason": None, "warnings": warnings}
    env_raw = str(env.get(ENV_NAME) or "").strip()
    if env_raw:
        path = Path(env_raw).expanduser()
        problem = registry_problem(path)
        if problem:
            return {"registry": None, "source": "env", "value": env_raw, "reason": f"{ENV_NAME} (environment) is not usable: {problem}", "warnings": warnings}
        return {"registry": path.resolve(), "source": "env", "value": env_raw, "reason": None, "warnings": warnings}
    seeded = seeded_registry_dir(data_dir)
    if _is_registry(seeded):
        return {"registry": seeded, "source": "seeded", "value": None, "reason": None, "warnings": warnings}
    outcome = last_seed(data_dir)
    why_not_seeded = (
        f"the gateway could not seed {seeded}: {outcome['error']}" if outcome and not outcome.get("ok")
        else f"{seeded} has not been seeded (it is seeded when the gateway starts)"
    )
    for cand in _checkout_candidates(checkout_root):
        if _is_registry(cand):
            warnings.append(f"{why_not_seeded}; reading the framework checkout's shelf at {cand} instead")
            return {"registry": cand.resolve(), "source": "checkout", "value": None, "reason": None, "warnings": warnings}
    return {"registry": None, "source": "none", "value": None, "reason": why_not_seeded, "warnings": warnings}


def shelf_setting_payload(data_dir: Path, *, checkout_root: Optional[Path] = None) -> Dict[str, Any]:
    """The `skills.shelf` row of GET /admin/runtime-config."""
    res = resolve_skills_shelf(data_dir, checkout_root=checkout_root)
    out: Dict[str, Any] = {
        "key": SETTING_KEY,
        "label": LABEL,
        "help": HELP,
        "env_name": ENV_NAME,
        "value": res["value"] if res["source"] in ("stored", "env") else None,
        "source": res["source"],
        "resolved": str(res["registry"]) if res["registry"] is not None else None,
        "available": res["registry"] is not None,
        "reason": res["reason"],
        "default_path": str(seeded_registry_dir(data_dir)),
        "bundled_version": bundled_version(),
        "cli": f"abstractgateway config set {SETTING_KEY} PATH",
    }
    if res["warnings"]:
        out["warnings"] = list(res["warnings"])
    return out
