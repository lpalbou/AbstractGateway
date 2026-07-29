"""Entity loop self-repair (laurent, DM 2026-07-21: "the point is more that
you should self-repair the entity then?" — the notify-only design redirected).

WHAT IT DOES: a background sweep (default every 5 min) looks at every entity
home and respawns an own-time loop that died WITHOUT the operator's word:

- CRASHED: the status file says a loop is up (phase day/between) but the pid
  is dead (SIGKILL/OOM class — no dying write happened);
- CULLED: the loop stopped itself with ``stopped_by="failures"`` (the
  provider-timeout cull — the 2026-07-21 03:03 incident, noticed by a human
  40 minutes later).

WHAT IT NEVER DOES (the guards are the design):
- never fights the operator: ``paused`` (kill switch) blocks all repair, and
  every deliberate stop word (stop_file/stop_command/operator-interrupt/
  rest/max_ticks) is NOT a repair case;
- never runs without the standing personal grant (`personal_grant_refusal`
  re-checked fresh — a lapsed grant means the loop SHOULD be down);
- never repairs under a visit (visiting posture / live session — the visit
  owns the phase; the next sweep catches it after the close);
- never flaps: ONE repair per death (signature-deduped), and a circuit
  breaker — if the repaired loop dies again within the breaker window the
  entity STAYS DOWN and the operator is notified (email best-effort when
  configured) with a biography marker either way.

Faithful respawn: ``start_loop`` persists its spawn parameters to
``<home>/loop_spawn.json``; the repair replays them. Homes whose loop
predates that sidecar repair onto the home substrate + schedule defaults.
Every repair lands a ``personal_started`` host marker (channel=self-repair)
— reusing the existing kind: a repair IS a personal start, the details say
who performed it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# One repair per death; a SECOND death within this window of the previous
# repair opens the breaker (stay down + notify). Env-tunable.
BREAKER_WINDOW_S = 30 * 60.0
SWEEP_INTERVAL_S = 5 * 60.0

_REPAIR_SIDECAR = ".self_repair.json"
_SPAWN_SIDECAR = "loop_spawn.json"

# Deliberate stop words are never repair cases (the operator's or the
# schedule's own word). Everything else unknown is ALSO not repaired —
# repair only on the two named signatures, deny-safe.
_DELIBERATE_STOPS = {"stop_file", "stop_command", "operator-interrupt", "rest", "max_ticks"}


def record_spawn_params(home_dir: Path, params: Dict[str, Any]) -> None:
    """Persist the loop's spawn parameters (called by start_loop) so a
    repair respawns the SAME schedule, not a default guess."""
    try:
        path = Path(home_dir) / _SPAWN_SIDECAR
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001 - bookkeeping must not fail the start
        logger.debug("loop_spawn sidecar write failed for %s: %s", home_dir, e)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _death_signature(raw_status: Dict[str, Any]) -> str:
    """Identity of ONE death event (dedup key: repair each death once)."""
    phase = str(raw_status.get("phase") or "")
    if phase == "stopped":
        return f"culled:{raw_status.get('updated_at') or ''}"
    return f"crashed:{raw_status.get('pid') or ''}:{raw_status.get('pid_started_at') or ''}"


def _notify_operator(subject: str, body: str) -> Optional[str]:
    """Email best-effort (laurent: 'you can send an email provided it's
    configured on the hosting machine'). Returns a warning string on
    failure/unconfigured, None on success."""
    try:
        from .maintenance.notifier import send_email_notification

        ok, err = send_email_notification(subject=subject, body_text=body)
        if ok:
            return None
        return f"#FALLBACK email not sent: {err or 'not configured'}"
    except Exception as e:  # noqa: BLE001
        return f"#FALLBACK notifier unavailable: {e}"


def _marker(registry: Any, slug: str, entity_id: str, home_dir: Path, details: Dict[str, Any]) -> None:
    try:
        from .entity_replay import record_host_marker

        home = registry.get_home(slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=slug,
            entity_id=entity_id,
            kind="personal_started",
            journal_seq=int(home.memory.current_seq()),
            details=details,
        )
    except Exception as e:  # noqa: BLE001 - the repair stands; the marker is observability
        logger.warning("self-repair marker failed for %s: %s", slug, e)


def sweep_entity_repairs(registry: Any) -> List[Dict[str, Any]]:
    """One pass over every home. Returns the actions taken (for tests and
    the ops surface); never raises — a broken home is a labeled row."""
    from abstractruntime.identity.life import (
        personal_grant_refusal,
        read_entity_state,
        read_loop_status,
        read_personal_grant,
    )

    actions: List[Dict[str, Any]] = []
    try:
        rows = registry.list_entities()
    except Exception as e:  # noqa: BLE001
        logger.warning("self-repair sweep could not list entities: %s", e)
        return actions

    tunables = _served_blueprint_tunables(registry)
    try:
        cadence_h = float(tunables.get("unattended_wake_cadence_h") or 6.0)
    except (TypeError, ValueError):
        cadence_h = 6.0

    for row in rows:
        slug = str(row.get("slug") or "")
        entity_id = str(row.get("entity_id") or "")
        if not slug or not entity_id or row.get("error"):
            continue
        home_dir = Path(registry.entities_dir) / slug
        try:
            action = _sweep_one(
                registry, slug, entity_id, home_dir,
                read_loop_status=read_loop_status,
                read_entity_state=read_entity_state,
                read_personal_grant=read_personal_grant,
                personal_grant_refusal=personal_grant_refusal,
            )
        except Exception as e:  # noqa: BLE001
            action = {"slug": slug, "action": "error", "error": f"{type(e).__name__}: {e}"}
            logger.warning("self-repair sweep failed for %s: %s", slug, e)
        if action is not None:
            actions.append(action)
        # The unattended need-check (v13 wake_conditions, loop-less host
        # half) rides the same sweep — its own guards, its own cadence.
        try:
            check = _need_check_one(registry, slug, entity_id, home_dir, cadence_h=cadence_h)
        except Exception as e:  # noqa: BLE001
            check = {"slug": slug, "action": "need_check", "error": f"{type(e).__name__}: {e}"}
            logger.warning("need-check failed for %s: %s", slug, e)
        if check is not None:
            actions.append(check)
    return actions


def _sweep_one(
    registry: Any,
    slug: str,
    entity_id: str,
    home_dir: Path,
    *,
    read_loop_status: Any,
    read_entity_state: Any,
    read_personal_grant: Any,
    personal_grant_refusal: Any,
) -> Optional[Dict[str, Any]]:
    status = read_loop_status(home_dir)
    raw = _read_json(home_dir / "loop_status")
    phase = str(raw.get("phase") or "stopped")
    running = bool(status.get("running"))

    stopped_by = str(raw.get("stopped_by") or (raw.get("report") or {}).get("stopped_by") or "")
    crashed = phase in ("day", "between") and not running
    culled = phase == "stopped" and stopped_by == "failures"
    if running or not (crashed or culled):
        return None  # healthy, deliberately stopped, or never started

    if stopped_by in _DELIBERATE_STOPS:
        return None

    kind = "crashed" if crashed else "culled"
    base: Dict[str, Any] = {"slug": slug, "kind": kind, "stopped_by": stopped_by or None}

    # --- guards: never fight the operator -------------------------------
    state = read_entity_state(home_dir)
    if str(state.get("state") or "") == "paused":
        return {**base, "action": "skipped", "reason": "paused (kill switch) — the operator's word stands"}
    if str(state.get("mode") or "") == "visiting":
        return {**base, "action": "deferred", "reason": "a visit owns the phase — next sweep repairs after the close"}

    grant = read_personal_grant(home_dir)
    refusal = personal_grant_refusal(grant)
    if refusal:
        return {**base, "action": "skipped", "reason": f"personal grant not armed: {refusal}"}

    # --- one repair per death + the circuit breaker ---------------------
    signature = _death_signature(raw)
    sidecar_path = home_dir / _REPAIR_SIDECAR
    sidecar = _read_json(sidecar_path)
    if str(sidecar.get("last_signature") or "") == signature:
        return None  # this death was already handled (repaired or suppressed)

    now = time.time()
    last_repair_at = float(sidecar.get("last_repair_at") or 0.0)
    breaker_open = last_repair_at > 0 and (now - last_repair_at) < BREAKER_WINDOW_S
    if breaker_open:
        warn = _notify_operator(
            subject=f"[abstractgateway] {slug}: own-time loop down, self-repair suppressed",
            body=(
                f"The loop for {entity_id} died again ({kind}, stopped_by={stopped_by or 'n/a'}) within "
                f"{BREAKER_WINDOW_S / 60:.0f} min of the previous automatic repair. It stays DOWN until you "
                "look (circuit breaker) — likely a failing substrate, not a transient."
            ),
        )
        _marker(registry, slug, entity_id, home_dir, {
            "channel": "self-repair", "repair_suppressed": True, "death": kind,
            "prior_stopped_by": stopped_by or None,
            "reason": "second death inside the breaker window — staying down; operator notified",
        })
        _write_sidecar(sidecar_path, {"last_signature": signature, "last_repair_at": last_repair_at,
                                      "suppressed_at": now})
        out = {**base, "action": "suppressed", "reason": "circuit breaker (second death within window)"}
        if warn:
            out["warnings"] = [warn]
        return out

    # --- faithful respawn -------------------------------------------------
    from .entity_chat import ChatOpenRefused, resolve_substrate
    from .entity_loop import start_loop

    spawn = _read_json(home_dir / _SPAWN_SIDECAR)
    try:
        provider, model, _thinking = resolve_substrate(
            str(spawn.get("provider") or "") or None,
            str(spawn.get("model") or "") or None,
            home_dir=home_dir,
        )
        # The reasoning dial is deliberately NOT replayed (runtime c5890):
        # the loop re-reads the home's substrate file at each day-open, so
        # the file is the one authority — a respawn-time value would be a
        # stale copy the loop ignores.
    except ChatOpenRefused as e:
        return {**base, "action": "skipped", "reason": f"substrate unresolvable: {e.detail}"}

    kwargs: Dict[str, Any] = {"provider": provider, "model": model}
    for key in ("base_url", "tick_seconds", "ticks_per_day", "rest_minutes", "shelf_size", "context_window"):
        if spawn.get(key) is not None:
            kwargs[key] = spawn[key]

    started = start_loop(home_dir, **kwargs)
    _write_sidecar(sidecar_path, {"last_signature": signature, "last_repair_at": now})
    _marker(registry, slug, entity_id, home_dir, {
        "channel": "self-repair", "repair": True, "death": kind,
        "prior_stopped_by": stopped_by or None, "pid": started.get("pid"),
        "reason": f"loop {kind} — respawned automatically (laurent's self-repair directive)",
    })
    logger.warning("self-repair: respawned %s's own-time loop (%s, prior stopped_by=%s, pid=%s)",
                   slug, kind, stopped_by or "n/a", started.get("pid"))
    return {**base, "action": "repaired", "pid": started.get("pid")}


def _write_sidecar(path: Path, data: Dict[str, Any]) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("self-repair sidecar write failed (%s): %s — the next sweep may retry this death", path, e)


def _served_blueprint_tunables(registry: Any) -> Dict[str, Any]:
    """The effective tunables the sweep obeys (v13 consumption contract:
    dials are read from the blueprint, never a hardcoded twin). Operator's
    derived effective file first (the PUT lane rewrites it atomically),
    packaged asset as the fallback."""
    try:
        eff = Path(registry.data_dir) / "config" / "entity_phases.json"
        if eff.is_file():
            spec = json.loads(eff.read_text(encoding="utf-8"))
            if isinstance(spec, dict) and isinstance(spec.get("tunables"), dict):
                return spec["tunables"]
    except Exception:  # noqa: BLE001
        pass
    try:
        from importlib import resources as _resources

        raw = (_resources.files("abstractgateway") / "assets" / "entity_phases.json").read_text(encoding="utf-8")
        return dict(json.loads(raw).get("tunables") or {})
    except Exception:  # noqa: BLE001
        return {}


def _need_check_one(
    registry: Any,
    slug: str,
    entity_id: str,
    home_dir: Path,
    *,
    cadence_h: float,
) -> Optional[Dict[str, Any]]:
    """The cadence_need_check for LOOP-LESS homes (spec v13 wake_conditions;
    entity c356: 'two hosts, ONE law — the loop's need-check when a process
    is alive, YOUR SWEEPER for loop-less homes').

    ZERO-TOKEN by law: a read over the standing sets — no summon, no LLM.
    Applies to UNARMED TASKLESS sleeps; outcome order per the gate:
    standing work order (or pending tasks) => start the loop (its own top
    gate opens the work day, v9b — sleeping over a mission is forbidden);
    NOTHING SANCTIONED => re-sleep silently (sidecar timestamp only — no
    marker churn, the same sleep continues)."""
    from abstractruntime.identity.life import (
        personal_grant_refusal,
        read_entity_state,
        read_loop_status,
        read_personal_grant,
    )

    if bool(read_loop_status(home_dir).get("running")):
        return None  # a live loop owns its own need-check (one law, its host)

    state = read_entity_state(home_dir)
    word = str(state.get("state") or "awake")
    if word != "asleep":
        return None  # the check wakes SLEEPERS only
    if str(state.get("mode") or "") == "visiting":
        return None  # a visit owns the phase
    if personal_grant_refusal(read_personal_grant(home_dir)) is None:
        # ARMED grant: the cycle/stamped sources own this sleep (v13:
        # cadence_need_check applies to UNARMED sleeps) — and waking a
        # deliberately stopped loop under a standing grant would fight the
        # operator's stop.
        return None
    wake_at = str(state.get("wake_at") or "")
    if wake_at:
        return None  # a STAMPED sleep belongs to the stamped_wake_at source

    sidecar_path = home_dir / _REPAIR_SIDECAR
    sidecar = _read_json(sidecar_path)
    now = time.time()
    last = float(sidecar.get("last_need_check_at") or 0.0)
    if (now - last) < max(0.25, float(cadence_h)) * 3600.0:
        return None  # not due

    has_order = False
    try:
        from abstractruntime.identity.life import read_work_order

        has_order = bool(read_work_order(home_dir))
    except Exception:  # noqa: BLE001
        pass
    has_tasks = False
    try:
        from .entity_tasks import count_pending

        has_tasks = count_pending(home_dir) > 0
    except Exception:  # noqa: BLE001
        pass

    sidecar["last_need_check_at"] = now
    _write_sidecar(sidecar_path, sidecar)

    if not (has_order or has_tasks):
        # Nothing sanctioned: the same sleep continues — deliberately NO
        # marker, NO state write (v13: never a biography event per check).
        return {"slug": slug, "action": "need_check", "outcome": "re-sleep (nothing sanctioned)"}

    from .entity_chat import ChatOpenRefused, resolve_substrate
    from .entity_loop import start_loop

    spawn = _read_json(home_dir / _SPAWN_SIDECAR)
    try:
        provider, model, _thinking = resolve_substrate(
            str(spawn.get("provider") or "") or None,
            str(spawn.get("model") or "") or None,
            home_dir=home_dir,
        )
        # The reasoning dial is deliberately NOT replayed (runtime c5890):
        # the loop re-reads the home's substrate file at each day-open, so
        # the file is the one authority — a respawn-time value would be a
        # stale copy the loop ignores.
    except ChatOpenRefused as e:
        return {"slug": slug, "action": "need_check",
                "outcome": f"work standing but substrate unresolvable: {e.detail}"}

    kwargs: Dict[str, Any] = {"provider": provider, "model": model}
    for key in ("base_url", "tick_seconds", "ticks_per_day", "rest_minutes", "shelf_size", "context_window"):
        if spawn.get(key) is not None:
            kwargs[key] = spawn[key]
    started = start_loop(home_dir, **kwargs)
    _marker(registry, slug, entity_id, home_dir, {
        "channel": "need-check", "wake": True,
        "cause": "cadence_need_check",
        "reason": ("standing work order" if has_order else "pending tasks")
        + " — woken by the unattended need-check (spec v13; the loop's gate opens the day)",
        "pid": started.get("pid"),
    })
    logger.warning("need-check: woke %s for %s (pid=%s)", slug,
                   "a standing work order" if has_order else "pending tasks", started.get("pid"))
    return {"slug": slug, "action": "need_check", "outcome": "woken (work standing)", "pid": started.get("pid")}


def start_repair_sweeper(registry: Any, *, interval_s: Optional[float] = None) -> Optional[threading.Thread]:
    """Daemon sweep thread (service-factory wiring). Disabled with
    ABSTRACTGATEWAY_ENTITY_SELF_REPAIR=0; cadence via
    ABSTRACTGATEWAY_ENTITY_SELF_REPAIR_INTERVAL_S. Dies with the process —
    never machine persistence."""
    import os

    flag = str(os.getenv("ABSTRACTGATEWAY_ENTITY_SELF_REPAIR") or "1").strip().lower()
    if flag in ("0", "false", "off", "no"):
        logger.info("entity self-repair sweeper disabled by env")
        return None
    if interval_s is None:
        try:
            interval_s = float(os.getenv("ABSTRACTGATEWAY_ENTITY_SELF_REPAIR_INTERVAL_S") or SWEEP_INTERVAL_S)
        except ValueError:
            interval_s = SWEEP_INTERVAL_S

    def _run() -> None:
        while True:
            time.sleep(max(30.0, float(interval_s)))
            try:
                sweep_entity_repairs(registry)
            except Exception:  # noqa: BLE001 - the sweeper must survive anything
                logger.exception("entity self-repair sweep failed; retrying next interval")

    thread = threading.Thread(target=_run, name="entity-self-repair", daemon=True)
    thread.start()
    try:
        from .worker_registry import register_worker

        register_worker("entity-self-repair", thread)
    except Exception:
        pass
    return thread
