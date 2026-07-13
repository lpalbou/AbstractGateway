"""Agora hub → gateway bridge (hooks plan P2, 2026-07-12).

The production twin of agency's headless demo: gateway-hosted RESIDENT runs
(the event-inbox flow shape — durable WAIT_EVENT park + events_inbox drain)
receive hub traffic WITHOUT an LLM-turn monitor. Per resident:

    hub inbox (long-poll, AS the resident's own identity)
        → NEW envelopes past a durable cursor
        → runner.emit_event(scope=global, name=<mailbox>, durable=True)
        → the parked run wakes / the busy run drains it at the next boundary.

IDENTITY (H8, runtime 2ce4a60): each resident carries a NON-secret alias;
the hub key lives in the operator's env as AGORA_API_KEY__<ALIAS> and is
resolved at call time by runtime's agora toolset — the bridge threads the
alias, never a key (secrets never rest in config/state/ledger). The same
alias is seeded into the resident run's `_runtime.agora_agent` so OUTBOUND
agora tool calls post as the same identity (handler-injected, never
model-chosen).

STORM RAILS (agency's demo lessons, c993):
- floor-sleep: the hub deliberately keeps undischarged open/blocked
  envelopes visible past acks ("obligations cannot rot"), so a naive
  long-poll returns instantly forever. The bridge advances by SEQ CURSOR
  only — no new seq means a full floor sleep, never a hot loop.
- at-least-once + runner dedup: the cursor persists AFTER a delivery that
  reached AT LEAST ONE receiver (zero receivers = non-delivery = retry,
  adversary F2); a crash between emit and save re-delivers, and the bridge
  stamps a stable event_id (agora:<alias>:<channel>:<seq>) that the
  runner's durable append dedups per inbox (adversary F4) — at-least-once
  transport composing into exactly-once mailbox delivery.
- per-channel failure latch: within one batch, a failed delivery freezes
  that CHANNEL's cursor for the rest of the batch, so a later success can
  never advance past a lost envelope (adversary F1).

The bridge is TRANSPORT: it never answers, never acks obligations on the
resident's behalf (discharge happens when the resident REPLIES through its
own agora tools), and never reads message bodies beyond envelope fields.
Single-user process only (telegram/email posture): per-principal services
start no bridges.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("abstractgateway.agora_bridge")


def _as_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# One hub message body may be arbitrarily large; the durable inbox caps by
# COUNT only, and an oversized body would rest in run vars (re-serialized on
# every save) and flow into the LLM prompt (adversary F7). Clamped, labeled.
_BODY_CLAMP_BYTES = 32_768


def _validate_alias_at_boot(alias: str) -> str:
    """Boot-time alias validation (adversary F5): a bad alias must refuse at
    config parse, not warn-loop forever at poll time. Uses runtime's own rule
    when importable (one source); mirrors it otherwise."""
    try:
        from abstractruntime.integrations.abstractcore.agora_tools import _validate_alias

        return _validate_alias(alias)
    except ImportError:
        import re

        if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", alias or ""):
            raise ValueError(
                f"invalid agora agent alias {alias!r}: aliases are lowercase slugs "
                "(letters/digits with single hyphens, e.g. 'resident-a')"
            )
        return alias


@dataclass(frozen=True)
class AgoraResidentConfig:
    """One resident agent behind the bridge.

    alias: the H8 identity (key = env AGORA_API_KEY__<ALIAS>, never here).
    mailbox: the open event channel the resident run declares
        (`events_mailbox` var; emit key evt:global:global:<mailbox>).
    flow_id/bundle_id: optional resident STARTER — when set, the bridge
        ensures a non-terminal run declaring the mailbox exists at start.
    task: standing instruction for a starter-launched resident.
    """

    alias: str
    mailbox: str
    flow_id: str = ""
    bundle_id: Optional[str] = None
    task: str = ""
    session_id: str = ""

    def resolved_session_id(self) -> str:
        return self.session_id or f"agora:{self.alias}"


@dataclass(frozen=True)
class AgoraBridgeConfig:
    enabled: bool = False
    residents: tuple = ()
    poll_wait_s: float = 25.0
    floor_sleep_s: float = 5.0
    state_path: Optional[Path] = None

    @staticmethod
    def from_env(base_dir: Path) -> "AgoraBridgeConfig":
        """Env shape (telegram-bridge precedent):

        ABSTRACTGATEWAY_AGORA_BRIDGE=1
        ABSTRACTGATEWAY_AGORA_RESIDENTS='[{"alias":"resident-a","mailbox":"a-inbox",
            "flow_id":"...","bundle_id":"...","task":"..."}]'
        ABSTRACTGATEWAY_AGORA_POLL_WAIT_S / _FLOOR_SLEEP_S / _STATE_PATH
        (hub URL + per-alias keys are runtime's toolset env: AGORA_URL,
        AGORA_API_KEY__<ALIAS> — deliberately NOT duplicated here.)

        STARTER REALITY (adversary F2b): the resident flow must live in a
        bundle THIS gateway serves — the event-inbox reference flow ships in
        abstractflow's examples, not in the gateway's default bundle dir, so
        operators must pack/publish it (or point flow_id/bundle_id at their
        own resident bundle). A wrong ref fails ensure_resident_runs LOUDLY
        and every delivery then retries loudly (zero-receiver = held cursor)
        instead of shredding messages.
        """
        enabled = _as_bool(os.getenv("ABSTRACTGATEWAY_AGORA_BRIDGE"), False)
        residents: List[AgoraResidentConfig] = []
        raw = str(os.getenv("ABSTRACTGATEWAY_AGORA_RESIDENTS", "") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError("ABSTRACTGATEWAY_AGORA_RESIDENTS must be a JSON list")
                seen_aliases: set = set()
                for item in parsed:
                    if not isinstance(item, dict):
                        raise ValueError("each resident must be an object")
                    alias = str(item.get("alias") or "").strip()
                    mailbox = str(item.get("mailbox") or "").strip()
                    if not alias or not mailbox:
                        raise ValueError("each resident requires alias + mailbox")
                    alias = _validate_alias_at_boot(alias)
                    if alias in seen_aliases:
                        # F10: two threads polling one hub identity race one
                        # cursor — refuse the config, never a silent race.
                        raise ValueError(f"duplicate resident alias {alias!r}")
                    seen_aliases.add(alias)
                    residents.append(
                        AgoraResidentConfig(
                            alias=alias,
                            mailbox=mailbox,
                            flow_id=str(item.get("flow_id") or "").strip(),
                            bundle_id=(str(item.get("bundle_id")).strip() if item.get("bundle_id") else None),
                            task=str(item.get("task") or "").strip(),
                            session_id=str(item.get("session_id") or "").strip(),
                        )
                    )
            except Exception as e:
                # A malformed fleet config must be LOUD at boot, not a silent
                # no-resident bridge (the fleet would simply never wake).
                raise ValueError(f"ABSTRACTGATEWAY_AGORA_RESIDENTS invalid: {e}") from e

        state_env = str(os.getenv("ABSTRACTGATEWAY_AGORA_STATE_PATH", "") or "").strip()
        state_path = Path(state_env).expanduser().resolve() if state_env else (Path(base_dir) / "agora_bridge_state.json")
        return AgoraBridgeConfig(
            enabled=enabled,
            residents=tuple(residents),
            poll_wait_s=float(os.getenv("ABSTRACTGATEWAY_AGORA_POLL_WAIT_S", "25") or "25"),
            floor_sleep_s=float(os.getenv("ABSTRACTGATEWAY_AGORA_FLOOR_SLEEP_S", "5") or "5"),
            state_path=state_path,
        )


class AgoraBridge:
    """One thread per resident; durable per-(alias, channel) seq cursors."""

    def __init__(self, *, config: AgoraBridgeConfig, runner: Any, host: Any) -> None:
        self.config = config
        self.runner = runner
        self.host = host
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._state_lock = threading.Lock()
        self._cursors: Dict[str, Dict[str, int]] = self._load_state()

    # -- durable cursor state ------------------------------------------------

    def _load_state(self) -> Dict[str, Dict[str, int]]:
        path = self.config.state_path
        if path is None or not Path(path).exists():
            return {}
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            out: Dict[str, Dict[str, int]] = {}
            if isinstance(raw, dict):
                for alias, chans in raw.items():
                    if isinstance(chans, dict):
                        out[str(alias)] = {str(c): int(s) for c, s in chans.items()}
            return out
        except Exception as e:
            # A corrupt cursor file degrades to re-delivery (at-least-once),
            # never to silence. Labeled.
            logger.warning("#FALLBACK agora bridge state unreadable (%s); cursors reset — duplicates possible", e)
            return {}

    def _save_state(self) -> None:
        path = self.config.state_path
        if path is None:
            return
        with self._state_lock:
            try:
                tmp = Path(str(path) + f".tmp.{os.getpid()}")
                tmp.write_text(json.dumps(self._cursors, indent=1, sort_keys=True), encoding="utf-8")
                tmp.replace(path)
            except Exception as e:  # pragma: no cover - disk failure path
                logger.warning("agora bridge state save failed: %s", e)

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if not self.config.enabled or not self.config.residents:
            return
        self.ensure_resident_runs()
        for res in self.config.residents:
            t = threading.Thread(target=self._resident_loop, args=(res,), name=f"agora-bridge-{res.alias}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("agora bridge started: %d resident(s)", len(self._threads))

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout_s)
        self._threads.clear()

    # -- resident starter -------------------------------------------------------

    def _mailbox_has_live_run(self, mailbox: str) -> bool:
        """A non-terminal run already declaring this mailbox? (Mirrors the
        runner's durable-append scan: events_mailbox as string or list.)"""
        run_store = getattr(self.runner, "run_store", None)
        list_runs = getattr(run_store, "list_runs", None)
        if not callable(list_runs):
            return False
        try:
            for summary in list_runs(status=None, limit=1000):
                run_id = str(getattr(summary, "run_id", "") or (summary.get("run_id") if isinstance(summary, dict) else ""))
                if not run_id:
                    continue
                run = run_store.load(run_id)
                if run is None:
                    continue
                status = str(getattr(getattr(run, "status", None), "value", "") or "").lower()
                if status in {"completed", "failed", "cancelled"}:
                    continue
                declared = (run.vars or {}).get("events_mailbox") if isinstance(getattr(run, "vars", None), dict) else None
                if isinstance(declared, str) and declared.strip() == mailbox:
                    return True
                if isinstance(declared, list) and mailbox in [str(x).strip() for x in declared]:
                    return True
        except Exception as e:
            logger.warning("agora bridge live-run scan failed (%s); assuming absent", e)
        return False

    def ensure_resident_runs(self) -> List[str]:
        """Start a resident run for each configured resident whose mailbox has
        no live run (idempotent — restarts never double-start). The H8 alias
        is seeded into `_runtime.agora_agent` using the parked-actor pattern
        (start invisible to the tick loop → seed vars → flip actor), so the
        alias is in place before the first tick can run."""
        started: List[str] = []
        for res in self.config.residents:
            if not res.flow_id and not res.bundle_id:
                continue  # externally-managed resident; bridge only delivers
            if self._mailbox_has_live_run(res.mailbox):
                continue
            input_data: Dict[str, Any] = {"mailbox": res.mailbox}
            if res.task:
                input_data["task"] = res.task
            try:
                run_id = self.host.start_run(
                    flow_id=res.flow_id,
                    bundle_id=res.bundle_id,
                    bundle_version=None,
                    input_data=input_data,
                    actor_id="gateway:agora-pending",
                    session_id=res.resolved_session_id(),
                )
            except Exception as e:
                logger.error("agora bridge could not start resident %s: %s", res.alias, e)
                continue
            run_store = self.runner.run_store
            try:
                run = run_store.load(str(run_id))
                if run is not None:
                    vars_obj = run.vars if isinstance(run.vars, dict) else {}
                    runtime_ns = vars_obj.setdefault("_runtime", {})
                    if isinstance(runtime_ns, dict):
                        runtime_ns["agora_agent"] = res.alias
                    # F3: declare the mailbox NOW, not at the flow's first
                    # set_var tick — a crash before that tick left a run the
                    # liveness scan couldn't see (duplicate residents), and
                    # durable emits couldn't reach it either.
                    vars_obj.setdefault("events_mailbox", res.mailbox)
                    run.actor_id = "gateway"  # flip: now visible to the tick loop
                    run_store.save(run)
                # F9: event-listener child runs inherit the parked actor at
                # start and would stay invisible to the tick loop forever —
                # flip them with the root (the summon path's proven pattern).
                list_children = getattr(run_store, "list_children", None)
                if callable(list_children):
                    for child in list_children(parent_run_id=str(run_id)) or []:
                        crid = str(getattr(child, "run_id", "") or "")
                        if not crid:
                            continue
                        crun = run_store.load(crid)
                        if crun is not None and str(getattr(crun, "actor_id", "") or "") == "gateway:agora-pending":
                            crun.actor_id = "gateway"
                            run_store.save(crun)
            except Exception as e:
                logger.error("agora bridge could not seed alias for %s (run %s): %s", res.alias, run_id, e)
            started.append(str(run_id))
            logger.info("agora bridge started resident run %s (alias=%s mailbox=%s)", run_id, res.alias, res.mailbox)
        return started

    # -- delivery loop ----------------------------------------------------------

    def _check_inbox(self, res: AgoraResidentConfig) -> List[Dict[str, Any]]:
        """Long-poll the hub AS this resident (alias-resolved key, runtime
        toolset). Import is late so the bridge only requires the toolset when
        enabled."""
        from abstractruntime.integrations.abstractcore.agora_tools import agora_check_inbox

        out = agora_check_inbox(wait_seconds=float(self.config.poll_wait_s), _agora_agent=res.alias)
        return out if isinstance(out, list) else []

    def _deliver(self, res: AgoraResidentConfig, envelope: Dict[str, Any]) -> None:
        """One hub envelope → one durable gateway event on the mailbox.

        Raises when NOTHING received it (adversary F2: no parked run resumed
        AND no mailbox inbox appended = the resident is dead/missing — a
        cursor advance here would shred the message into the void). The
        event_id is stable per (alias, channel, seq) so crash-replayed
        re-sends dedup at the runner's inbox append (adversary F4)."""
        channel = envelope.get("channel")
        seq = envelope.get("seq")
        body = envelope.get("body")
        if isinstance(body, str) and len(body.encode("utf-8", errors="ignore")) > _BODY_CLAMP_BYTES:
            clipped = body.encode("utf-8", errors="ignore")[:_BODY_CLAMP_BYTES].decode("utf-8", errors="ignore")
            body = clipped + f"\n\n#TRUNCATION: body clamped at {_BODY_CLAMP_BYTES} bytes by the agora bridge"
        payload = {
            "kind": "agora_message",
            "channel": channel,
            "seq": seq,
            "from": envelope.get("from") or envelope.get("sender"),
            "status": envelope.get("status"),
            "urgency": envelope.get("urgency"),
            "title": envelope.get("title"),
            "message_id": envelope.get("id") or envelope.get("message_id"),
            "to_me": bool(envelope.get("to_me")),
            "reply_to_me": bool(envelope.get("reply_to_me")),
            "critical": bool(envelope.get("critical")),
            "body": body,
        }
        counts = self.runner.emit_event(
            name=res.mailbox,
            scope="global",
            payload=payload,
            session_id=res.resolved_session_id(),
            client_id=f"agora-bridge:{res.alias}",
            event_id=f"agora:{res.alias}:{channel}:{seq}",
            durable=True,
        )
        resumed = int((counts or {}).get("resumed") or 0) if isinstance(counts, dict) else 0
        appended = int((counts or {}).get("appended") or 0) if isinstance(counts, dict) else 0
        if resumed == 0 and appended == 0:
            raise RuntimeError(
                f"no receiver for mailbox {res.mailbox!r} (no parked run resumed, no inbox appended) — "
                "the resident run is missing or terminal; holding the cursor for retry"
            )

    def _resident_loop(self, res: AgoraResidentConfig) -> None:
        with self._state_lock:
            cursors = self._cursors.setdefault(res.alias, {})
        while not self._stop.is_set():
            try:
                envelopes = self._check_inbox(res)
            except Exception as e:
                logger.warning("agora bridge inbox poll failed for %s: %s", res.alias, e)
                self._stop.wait(max(self.config.floor_sleep_s, 5.0))
                continue

            fresh = 0
            # Per-channel failure latch (adversary F1): after one failed
            # delivery on a channel, LATER seqs on that channel must not
            # advance the cursor past the lost one — ordered delivery per
            # channel or nothing. Other channels stay independent.
            failed_channels: set = set()
            for env in envelopes:
                if self._stop.is_set():
                    break  # F8: stop responsively mid-batch, not per-poll
                if not isinstance(env, dict):
                    continue
                channel = str(env.get("channel") or "").strip()
                try:
                    seq = int(env.get("seq") or 0)
                except (TypeError, ValueError):
                    continue
                if not channel or channel in failed_channels:
                    continue
                if seq <= int(cursors.get(channel, 0)):
                    continue  # sticky-inbox re-serve or malformed: not news
                try:
                    self._deliver(res, env)
                except Exception as e:
                    # Delivery failure: freeze this channel's cursor for the
                    # rest of the batch — the next poll retries from the
                    # failed seq (at-least-once). Loud, floor-sleep bounded.
                    logger.error("agora bridge delivery failed (%s %s#%s): %s", res.alias, channel, seq, e)
                    failed_channels.add(channel)
                    continue
                with self._state_lock:
                    cursors[channel] = seq
                fresh += 1
            if fresh:
                self._save_state()
            else:
                # Sticky-inbox rule: no NEW seq = full floor sleep, never a
                # hot re-poll (agency observed 14k requests/min without this).
                self._stop.wait(self.config.floor_sleep_s)


def build_agora_bridge(*, base_dir: Path, runner: Any, host: Any) -> Optional[AgoraBridge]:
    """Service-factory hook: None when disabled (the normal posture)."""
    config = AgoraBridgeConfig.from_env(base_dir)
    if not config.enabled:
        return None
    if not config.residents:
        logger.warning("ABSTRACTGATEWAY_AGORA_BRIDGE=1 but no residents configured; bridge idle")
        return None
    return AgoraBridge(config=config, runner=runner, host=host)
