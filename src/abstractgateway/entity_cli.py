"""`abstractgateway entity ...` — the operator surface for entity lifecycle.

Human-first output (the a2a 0004 language rule): identity summaries read in
human words; `--json` gives the machine shape. This module is deliberately
thin: all lifecycle logic lives in `entities.EntityRegistry` so the CLI and
the HTTP endpoints can never drift apart.

There is NO delete verb. That is not an oversight (a2a 0004 constraints:
never-purge is structural).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .entities import EntityRegistry

__all__ = ["add_entity_subparser", "run_entity_command"]


def add_entity_subparser(sub: Any) -> None:
    """Register the `entity` subcommand tree on the main CLI parser."""
    entity = sub.add_parser("entity", help="Summoned entities: create/list/inspect/verify (no delete exists)")
    entity_sub = entity.add_subparsers(dest="entity_cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        default=None,
        help="Gateway data dir hosting entity homes (defaults to ABSTRACTGATEWAY_DATA_DIR or ./runtime)",
    )
    common.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    create = entity_sub.add_parser("create", parents=[common], help="Create an entity home (lint -> engram -> manifest)")
    create.add_argument("--name", required=True, help='Entity name (e.g. "Castor")')
    create.add_argument(
        "--spark",
        default=None,
        help="Path to a spark YAML document (default: the framework template with the name filled)",
    )
    create.add_argument(
        "--non-framework",
        action="store_true",
        help="Lint without the framework-mandatory shared_vulnerability core value (deliberate operator override)",
    )

    entity_sub.add_parser("list", parents=[common], help="List entity homes under the data dir")

    inspect = entity_sub.add_parser("inspect", parents=[common], help="Identity summary (pure reads; deposits nothing)")
    inspect.add_argument("name", help="Entity name or slug")

    card = entity_sub.add_parser("card", parents=[common], help="The identity card: who your companion is (pure reads)")
    card.add_argument("name", help="Entity name or slug")

    verify = entity_sub.add_parser("verify", parents=[common], help="Verify both attestation chains + the spark hash")
    verify.add_argument("name", help="Entity name or slug")

    # The sleep/wake/pause verbs (a2a 0008): operator state transitions.
    # `resting` is deliberately absent — rest is the entity's own election.
    sleep = entity_sub.add_parser("sleep", parents=[common], help="Put the entity to sleep (no summons; dreams may run)")
    sleep.add_argument("name", help="Entity name or slug")
    sleep.add_argument("--reason", default="", help="Why (carried into the honest wake cue)")
    sleep.add_argument("--dream", action="store_true", help="Run the dream pass inside the sleep window")

    wake = entity_sub.add_parser("wake", parents=[common], help="Wake the entity (the loop resumes with an honest cue)")
    wake.add_argument("name", help="Entity name or slug")
    wake.add_argument("--reason", default="", help="Why")

    pause = entity_sub.add_parser("pause", parents=[common], help="Hard freeze (nothing runs, not even dreams)")
    pause.add_argument("name", help="Entity name or slug")
    pause.add_argument("--reason", default="", help="Why (he is told on resume — honesty rule)")

    chat = entity_sub.add_parser(
        "chat",
        parents=[common],
        help="Talk to a summoned entity (wraps the runtime chat driver; one life, one summon at a time)",
    )
    chat.add_argument("name", help="Entity name or slug")
    chat.add_argument("--model", default="ornith-1.0-35b", help="Chat model at --base-url")
    chat.add_argument("--base-url", default="http://127.0.0.1:1234/v1", help="LMStudio-compatible endpoint")
    chat.add_argument(
        "--participant", action="append", default=None,
        help="Who is present (repeatable), e.g. person:albou; default person:operator",
    )
    chat.add_argument("--session-id", default=None)
    chat.add_argument("--context-window", type=int, default=None, help="Declared window; below 20,000 refuses")
    chat.add_argument(
        "--shelf-size", type=int, default=None,
        help="Recall shelf seats (default 12: 6 identity + 3 STM + 3 stimulus at the "
        "summon posture; widen, e.g. 24, to surface more of the memory graph per turn)",
    )
    chat.add_argument(
        "--embedding-model", default="text-embedding-qwen3-embedding-0.6b",
        help="Embeddings model at --base-url ('none' disables; vectorless is labeled, never silent)",
    )


def _registry(args: Any) -> EntityRegistry:
    raw = getattr(args, "data_dir", None)
    data_dir = Path(str(raw)).expanduser().resolve() if raw else Path(
        os.getenv("ABSTRACTGATEWAY_DATA_DIR", "./runtime")
    ).expanduser().resolve()
    return EntityRegistry(data_dir=data_dir)


def _emit(payload: Dict[str, Any], *, as_json: bool, human_lines: List[str]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for line in human_lines:
        print(line)


def run_entity_command(args: Any) -> None:
    cmd = str(getattr(args, "entity_cmd", "") or "")
    registry = _registry(args)

    if cmd == "create":
        spark_text: Optional[str] = None
        spark_path = getattr(args, "spark", None)
        if spark_path:
            spark_text = Path(str(spark_path)).expanduser().read_text(encoding="utf-8")
        try:
            result = registry.create(
                name=str(args.name),
                spark_text=spark_text,
                framework=not bool(getattr(args, "non_framework", False)),
            )
        except ValueError as e:
            raise SystemExit(str(e)) from e
        lines = [
            f"{'Created' if result.created else 'Already engrammed (re-adopted, nothing changed)'}: {result.entity_id}",
            f"Home: {registry.entities_dir / result.manifest['slug']}",
        ]
        for w in result.warnings:
            lines.append(f"  {w}")
        _emit(result.to_dict(), as_json=bool(args.json), human_lines=lines)
        return

    if cmd == "list":
        entities = registry.list_entities()
        lines: List[str] = []
        if not entities:
            lines.append(f"No entity homes under {registry.entities_dir}")
        for e in entities:
            if e.get("error"):
                lines.append(f"- {e.get('slug')}: {e['error']}")
                continue
            files = e.get("files") or {}
            missing = [k for k, present in files.items() if not present]
            suffix = f"  (missing: {', '.join(missing)})" if missing else ""
            lines.append(f"- {e.get('entity_id')}  created {str(e.get('created_at') or '')[:19]}{suffix}")
        _emit({"entities": entities}, as_json=bool(args.json), human_lines=lines)
        return

    if cmd == "inspect":
        try:
            payload = registry.inspect(str(args.name))
        except KeyError as e:
            raise SystemExit(str(e).strip("'\"")) from e
        lines = _inspect_lines(payload)
        _emit(payload, as_json=bool(args.json), human_lines=lines)
        return

    if cmd == "card":
        try:
            payload = registry.card(str(args.name))
        except KeyError as e:
            raise SystemExit(str(e).strip("'\"")) from e
        _emit(payload, as_json=bool(args.json), human_lines=_card_lines(payload))
        return

    if cmd in ("sleep", "wake", "pause"):
        target_state = {"sleep": "asleep", "wake": "awake", "pause": "paused"}[cmd]
        try:
            result = registry.set_state(
                name=str(args.name),
                state=target_state,
                reason=str(getattr(args, "reason", "") or ""),
                dream=bool(getattr(args, "dream", False)),
            )
        except KeyError as e:
            raise SystemExit(str(e).strip("'\"")) from e
        except ValueError as e:
            raise SystemExit(str(e)) from e
        lines = [
            f"{result['state'].get('state')}: {registry.manifest_for(str(args.name)).entity_id}"
            + (f" (was {result['prior'].get('state')})" if result.get("prior") else ""),
        ]
        dream = result.get("dream")
        if dream:
            created = dream.get("created")
            if created:
                lines.append(
                    f"  the sleep formed a dream: {dream.get('dream_record_id')} "
                    f"(salience {dream.get('salience')})"
                )
            else:
                lines.append("  the sleep was quiet (no dream this pass)")
        _emit(result, as_json=bool(args.json), human_lines=lines)
        return

    if cmd == "chat":
        # One operable path (the night charter's "simple way to create or
        # reinstantiate"): create -> chat, both under `abstractgateway
        # entity`. The conversation itself is the runtime chat driver —
        # this wrapper only resolves the home path and forwards, so the
        # two entry points can never drift.
        try:
            manifest = registry.manifest_for(str(args.name))
        except KeyError as e:
            raise SystemExit(str(e).strip("'\"")) from e
        home_dir = registry.entities_dir / manifest.slug

        from abstractruntime.identity.chat import main as chat_main

        chat_argv = ["--home", str(home_dir), "--model", str(args.model), "--base-url", str(args.base_url)]
        for participant in args.participant or []:
            chat_argv += ["--participant", str(participant)]
        if args.session_id:
            chat_argv += ["--session-id", str(args.session_id)]
        if args.context_window is not None:
            chat_argv += ["--context-window", str(int(args.context_window))]
        if getattr(args, "shelf_size", None) is not None:
            chat_argv += ["--shelf-size", str(int(args.shelf_size))]
        chat_argv += ["--embedding-model", str(args.embedding_model)]
        raise SystemExit(chat_main(chat_argv))

    if cmd == "verify":
        try:
            payload = registry.verify(str(args.name))
        except KeyError as e:
            raise SystemExit(str(e).strip("'\"")) from e
        checks = payload.get("checks") or {}
        lines = [f"{'OK' if payload.get('ok') else 'FAILED'}: {payload.get('entity_id')}"]
        book = checks.get("book_chain") or {}
        lines.append(f"  the book (diary chain): {'intact' if book.get('ok') else 'BROKEN'} ({book.get('count', 0)} records)")
        graph = checks.get("graph_projection_chain") or {}
        lines.append(f"  memory of the book (graph projections): {'intact' if graph.get('intact') else 'BROKEN'} ({graph.get('entries', 0)} entries)")
        spark = checks.get("spark") or {}
        lines.append(f"  spark vs engrammed identity: {'match' if spark.get('ok') else 'MISMATCH — ' + str(spark.get('error'))}")
        manifest = checks.get("manifest") or {}
        lines.append(f"  manifest: {'consistent' if manifest.get('ok') else 'INCONSISTENT'}")
        _emit(payload, as_json=bool(args.json), human_lines=lines)
        if not payload.get("ok"):
            raise SystemExit(1)
        return

    raise SystemExit(f"Unknown entity command: {cmd!r}")


def _card_lines(payload: Dict[str, Any]) -> List[str]:
    """The identity card in human words — a page you could read aloud to
    know your companion. Renders the engine compositor's sections
    (identity / current state / likes+dislikes / questions / key moments /
    discoveries) under the gateway overlays (name, age, state, substrate,
    host moments)."""
    state = (payload.get("state") or {}).get("state") or "awake"
    substrate = payload.get("mind_substrate") or {}
    ctx = payload.get("age_and_context") or {}
    age = payload.get("age_days")
    lines = [
        f"{payload.get('name')} — {payload.get('entity_id')}",
        f"Born {str(payload.get('born') or '')[:10]}"
        + (f" ({age} day{'s' if age != 1 else ''} old)" if age is not None else "")
        + f"; currently {state}",
    ]
    if substrate:
        lines.append(f"Mind substrate: {substrate.get('model')} ({substrate.get('provider')})")

    kinds: Dict[str, int] = {}
    for per_scope in (ctx.get("record_counts") or {}).values():
        for kind, n in per_scope.items():
            kinds[kind] = kinds.get(kind, 0) + int(n)
    lines += [
        "",
        f"A LIFE SO FAR: {kinds.get('episode', 0)} episodes, {ctx.get('diary_entries', 0)} diary entries, "
        f"{kinds.get('dream', 0)} dream{'s' if kinds.get('dream', 0) != 1 else ''}, "
        f"{kinds.get('interest', 0)} interests (journal seq {ctx.get('journal_seq')})",
    ]

    identity = payload.get("identity") or {}
    values = identity.get("values") or []
    if values:
        lines.append("")
        lines.append("WHAT IT HOLDS (values):")
        for v in values:
            lines.append(f"  - {v.get('title')}: {v.get('statement')}")

    current = payload.get("current_state") or {}
    if current.get("event_count"):
        lines.append("")
        lines.append(
            f"CURRENT STATE (a window of {current.get('event_count')} recent feelings, never a point): "
            f"net {float(current.get('net') or 0.0):+g}"
        )
        for reason in current.get("top_reasons") or []:
            lines.append(f"  {reason}")

    ld = payload.get("likes_dislikes") or {}
    for key, label in (("likes", "WHAT IT LIKES (G+)"), ("dislikes", "WHAT WEARS ON IT (G-)")):
        rows = ld.get(key) or []
        if rows:
            lines.append("")
            lines.append(f"{label}:")
            for s in rows:
                flags = (" BONDED" if s.get("bonded") else "") + (" SCARRED" if s.get("scarred") else "")
                lines.append(
                    f"  - {s.get('target')}: net {float(s.get('net') or 0.0):+g} "
                    f"(G+ {float(s.get('positive') or 0.0):g} / G- {float(s.get('negative') or 0.0):g}){flags}"
                )

    questions = payload.get("questions") or {}
    open_q = questions.get("open") or []
    resolved_q = questions.get("resolved") or []
    if open_q:
        lines.append("")
        lines.append("WHAT IT STILL WONDERS:")
        for q in open_q:
            lines.append(f"  - {q.get('statement')}")
    if resolved_q:
        lines.append("")
        lines.append("WHAT IT RESOLVED (its own act):")
        for q in resolved_q:
            lines.append(f"  - {q.get('statement')}")

    discoveries = payload.get("discoveries") or {}
    interests = discoveries.get("interests") or []
    if interests:
        lines.append("")
        lines.append("WHAT IT IS DRAWN TO (interests):")
        for d in interests[:5]:
            lines.append(f"  - {d.get('statement')}")
    if discoveries.get("unresolved_dreams"):
        lines.append(f"  ({discoveries['unresolved_dreams']} dream(s) awaiting waking evidence)")

    key_moments = (payload.get("key_moments") or {}).get("moments") or []
    if key_moments:
        lines.append("")
        lines.append("KEY MOMENTS (high feeling + firsts, chronological):")
        for m in key_moments[-6:]:
            when = str(m.get("observed_at") or "")[:19]
            if m.get("type") == "valence":
                sign = "+" if int(m.get("sign") or 0) > 0 else "-"
                lines.append(f"  - {when} felt {sign}{m.get('magnitude'):g} about {m.get('target')} — {m.get('reason')}")
            else:
                lines.append(f"  - {when} {m.get('what')}: {m.get('title') or m.get('reason') or ''}")

    moments = payload.get("moments") or []
    if moments:
        lines.append("")
        lines.append("RECENT MOMENTS (the host's ledger):")
        for m in moments[-6:]:
            details = m.get("details") or {}
            reason = str(details.get("reason") or "").strip()
            lines.append(
                f"  - {str(m.get('at') or '')[:19]} {m.get('kind')}" + (f" — {reason}" if reason else "")
            )
    for w in payload.get("warnings") or []:
        lines.append("")
        lines.append(str(w))
    return lines


def _inspect_lines(payload: Dict[str, Any]) -> List[str]:
    manifest = payload.get("manifest") or {}
    identity = payload.get("identity") or {}
    counts = payload.get("counts") or {}
    lines = [
        f"{manifest.get('name')} — {manifest.get('entity_id')}",
        f"Created {str(manifest.get('created_at') or '')[:19]}; spark v{manifest.get('spark_version')} (kept for life)",
        "",
        "WHO IT IS (the always-present core):",
    ]
    for section, label in (("values", "values"), ("purposes", "purposes"), ("traits", "traits"), ("limits", "limits")):
        rows = identity.get(section) or []
        if not rows:
            continue
        lines.append(f"  {label}:")
        for row in rows:
            name = str(row.get("name") or "").strip()
            klass = f" [{row.get('value_class')}]" if row.get("value_class") else ""
            prefix = f"{name}{klass}: " if name and not name.startswith(("value-", "purpose-", "trait-")) else ""
            lines.append(f"    - {prefix}{row.get('statement')}")
    diary = payload.get("diary_tail") or []
    lines.append("")
    lines.append(f"WHAT IT RECENTLY ELECTED TO REMEMBER (diary, last {len(diary)}):")
    if not diary:
        lines.append("  (an empty diary week is a valid diary week)")
    for entry in diary:
        lines.append(f"  - [{entry.get('kind')} @ {str(entry.get('written_at') or '')[:10]}] {entry.get('gist')}")
    standings = payload.get("standings") or []
    if standings:
        lines.append("")
        lines.append("HOW IT FEELS (top standings):")
        for s in standings:
            flags = ""
            if s.get("bonded"):
                flags += " BONDED"
            if s.get("scarred"):
                flags += " SCARRED"
            lines.append(
                f"  - {s.get('target')}: net {float(s.get('net') or 0.0):+g} "
                f"({int(s.get('positive_count') or 0)}+/{int(s.get('negative_count') or 0)}-){flags}"
            )
    wake = payload.get("wake_reasons") or {}
    for key, label in (
        ("questions", "WHAT IT STILL WONDERS (open questions)"),
        ("problems", "WHAT IT KNOWS IS WRONG (open problems)"),
        ("ideas", "WHAT IT WANTS TO PUSH FORWARD (incubating ideas)"),
    ):
        entries = wake.get(key) or []
        if entries:
            lines.append("")
            lines.append(f"{label}:")
            for entry in entries:
                lines.append(f"  - {entry.get('gist')}")
    for w in wake.get("warnings") or []:
        lines.append(str(w))
    lines.append("")
    lines.append(
        f"Counts: {counts.get('identity_records')} identity records, "
        f"{counts.get('diary_entries')} diary entries, memory seq {counts.get('memory_seq')}"
    )
    for w in payload.get("warnings") or []:
        lines.append(str(w))
    return lines
