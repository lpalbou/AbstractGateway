"""Boot environment scanner (env-kill phase 1; warning, NEVER a gate).

Operator rulings this rides:
- dm#177 / c4157: behavior env vars migrate to console config; the incident
  class is an inherited foreign var silently steering the process
  (ABSTRACTVOICE_TTS_ENGINE overriding configuration).
- c4211 (URGENT live contamination): the serving gateway inherited a foreign
  AGORA_API_KEY from its launching shell — runtime's agora toolset armed
  itself with ANOTHER SEAT'S identity. Remediation is three-seated (framework:
  launcher sanitization; runtime: tool identity from explicit config); the
  gateway's half is THIS scanner: see the contamination at boot and SAY so.
- c4305 re-scope: the scanner WARNS and surfaces — it never refuses to boot,
  never unsets, never mutates the environment.

What it does: one pass over `os.environ` at boot, classified against the
declared registry (`env_registry.classify_env_var`), producing a report of
NAMES ONLY (never values — secret hygiene: presence, never material).

Scan set (adversary F1 2026-07-22): framework prefixes (ABSTRACT*/AGORA_*)
UNION every explicitly-declared registry name — declared FOREIGN rows live
outside the prefixes too (OPENAI_BASE_URL retargets every OpenAI call as
surely as ABSTRACTVOICE_TTS_ENGINE swaps the TTS engine).

Disclosure discipline (adversary F2+F4): full NAMES go to the operator's LOG
(the boot banner) and to the in-process `service.env_scan` report (a future
authenticated console read); the PUBLIC surfaces (`boot_warnings` on
/api/health) carry COUNTS-plus-summary only — an unauthenticated probe must
not learn which credentials this process holds. Buckets are capped so a
hostile launcher cannot flood logs or the health payload.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Mapping, Optional

from .env_registry import (
    BEHAVIOR,
    FOREIGN,
    LEGACY_ALIAS,
    SECRET,
    classify_env_var,
    declared_explicit_names,
)

logger = logging.getLogger(__name__)

# Framework namespaces scanned wholesale. PATH/HOME and the rest of a login
# environment are none of our business.
_FRAMEWORK_PREFIXES: tuple = (
    "ABSTRACT",   # every package namespace (ABSTRACTGATEWAY_, ABSTRACTCORE_, ABSTRACT_TELEGRAM_, ...)
    "AGORA_",     # hub credentials/identity — the c4211 contamination namespace
)

# Names dangerous by PRESENCE regardless of registry classification (adversary
# F3: the check must not go dark if these ever gain a registry row) — lower
# packages in this process read them, and an inherited value swaps
# identity/routing silently.
_PRESENCE_HAZARDS: tuple = (
    "AGORA_API_KEY",   # c4211: cross-identity tool calls on the hub
    "AGORA_AGENT_ID",  # identity override for the in-process toolset
    "AGORA_URL",       # silently retargets the hub
)

# Per-bucket cap on names carried in the report/log lines (adversary F2:
# a launcher exporting thousands of ABSTRACT* names must not flood the log
# or bloat any payload). The counts stay exact; only the name lists truncate.
_BUCKET_NAME_CAP = 20

# Process env is process-wide: scan ONCE and share across per-principal
# service factories (adversary F8 — N principals re-logging identical
# banners is noise, not information).
_scan_lock = threading.Lock()
_scan_memo: Optional[Dict[str, Any]] = None


def _is_framework_name(name: str) -> bool:
    return any(name.startswith(p) for p in _FRAMEWORK_PREFIXES)


def _capped(names: List[str]) -> List[str]:
    if len(names) <= _BUCKET_NAME_CAP:
        return names
    return names[:_BUCKET_NAME_CAP] + [f"+{len(names) - _BUCKET_NAME_CAP} more"]


def scan_process_env(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Classify every framework-relevant env var present in this process.

    Pure read: never mutates the environment, never raises (a scanner that
    can kill boot is a gate wearing a costume). Values NEVER enter the
    report — only names and classifications.
    """
    try:
        env = environ if environ is not None else os.environ
        declared = set(declared_explicit_names())
        names = sorted(
            n for n in env.keys()
            if isinstance(n, str) and (_is_framework_name(n) or n in declared)
        )

        foreign: List[Dict[str, str]] = []
        legacy: List[Dict[str, str]] = []
        undeclared: List[str] = []
        behavior_names: List[str] = []
        secret_present = 0

        for name in names:
            # Presence hazards warn INDEPENDENT of registry classification
            # (F3): a future registry row for AGORA_API_KEY must not silence
            # the c4211 incident class.
            if name in _PRESENCE_HAZARDS:
                foreign.append({
                    "name": name,
                    "owner": "agora",
                    "note": "identity/routing material for the in-process hub toolset; "
                            "an inherited value acts as ANOTHER seat (c4211 incident class)",
                })
                continue
            spec = classify_env_var(name)
            if spec is None:
                undeclared.append(name)
                continue
            if spec.klass == FOREIGN:
                row = {"name": name, "owner": spec.owner}
                if spec.note:
                    row["note"] = spec.note
                foreign.append(row)
            elif spec.klass == LEGACY_ALIAS:
                row = {"name": name, "alias_of": spec.alias_of or ""}
                if spec.note:
                    row["note"] = spec.note
                legacy.append(row)
            elif spec.klass == BEHAVIOR:
                behavior_names.append(name)
            elif spec.klass == SECRET:
                secret_present += 1
            # DEPLOYMENT rows are the legitimate env surface — not reported.

        return {
            "scanned": len(names),
            "foreign": foreign,
            "legacy_alias": legacy,
            "undeclared": undeclared,
            "behavior_env_count": len(behavior_names),
            "behavior_env_names": behavior_names,
            "secret_present_count": secret_present,
        }
    except Exception as e:  # noqa: BLE001 - never a gate, never a boot killer
        logger.warning("boot env scan failed (non-fatal): %s", type(e).__name__)
        return {"scanned": 0, "foreign": [], "legacy_alias": [], "undeclared": [],
                "behavior_env_count": 0, "behavior_env_names": [],
                "secret_present_count": 0, "error": type(e).__name__}


def scan_process_env_once() -> Dict[str, Any]:
    """The memoized process-wide scan (F8): principals share one report."""
    global _scan_memo
    with _scan_lock:
        if _scan_memo is None:
            _scan_memo = scan_process_env()
        return _scan_memo


def log_env_scan_banner(report: Dict[str, Any]) -> None:
    """The operator-facing boot banner: full names (capped per bucket) go to
    the LOG — the operator's stderr is an authenticated surface; /api/health
    is not (F4). Silence is the healthy state."""
    try:
        foreign = [str(r.get("name")) for r in (report.get("foreign") or [])]
        for name in _capped(foreign):
            logger.warning(
                "#ENV foreign var present: %s — another package's behavior/identity is riding "
                "this process from the launching shell; prefer the owner's config, unset from the launcher",
                name,
            )
        und = [str(n) for n in (report.get("undeclared") or [])]
        for name in _capped(und):
            if name.startswith("AGORA_"):
                # F6: AGORA_* names were never gateway vars — foreign-hub wording.
                logger.warning(
                    "#ENV foreign hub var present: %s — agora-owned material in the process env; "
                    "the in-process toolset may read it (c4211 class)",
                    name,
                )
            else:
                logger.warning(
                    "#ENV undeclared framework var present: %s — not in the gateway env registry "
                    "(retired name still exported, or a read nobody declared)",
                    name,
                )
        legacy = [str(r.get("name")) for r in (report.get("legacy_alias") or [])]
        if legacy:
            logger.warning(
                "#ENV %s legacy alias spelling(s) present (%s) — phase-4 removal class",
                len(legacy), ", ".join(_capped(legacy)),
            )
        behavior_count = int(report.get("behavior_env_count") or 0)
        if behavior_count:
            logger.warning(
                "#ENV %s behavior var(s) configured via env — these migrate to console/CLI "
                "config (env demotes to #FALLBACK, then dies; dm#177)",
                behavior_count,
            )
        if report.get("error"):
            logger.warning("#ENV boot env scan itself failed (%s) — inventory unavailable this boot", report["error"])
    except Exception:  # noqa: BLE001
        pass


def env_scan_summary_warnings(report: Dict[str, Any]) -> List[str]:
    """The PUBLIC boot_warnings lines: counts only, no names (F4 — an
    unauthenticated /api/health caller must not learn which credentials this
    process holds; the names live in the log banner and the authenticated
    console read). Empty report = no lines."""
    lines: List[str] = []
    try:
        n_foreign = len(report.get("foreign") or [])
        n_und = len(report.get("undeclared") or [])
        n_legacy = len(report.get("legacy_alias") or [])
        behavior_count = int(report.get("behavior_env_count") or 0)
        if n_foreign:
            lines.append(
                f"#ENV {n_foreign} foreign env var(s) present in this process — see the boot "
                "log banner for names; foreign vars ride behavior/identity from the launching shell"
            )
        if n_und:
            lines.append(f"#ENV {n_und} undeclared framework env var(s) present — see the boot log banner")
        if n_legacy:
            lines.append(f"#ENV {n_legacy} legacy alias env spelling(s) present — phase-4 removal class")
        if behavior_count:
            lines.append(
                f"#ENV {behavior_count} behavior var(s) configured via env — migrating to console/CLI config (dm#177)"
            )
        if report.get("error"):
            # F5: a broken scanner must not impersonate healthy silence.
            lines.append(f"#ENV boot env scan failed ({report['error']}) — env inventory unavailable this boot")
    except Exception:  # noqa: BLE001
        pass
    return lines
