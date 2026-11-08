"""`abstractgateway entity identity get|set` and `abstractgateway entity record` (framework ADR 0052).

Both go THROUGH the gateway's routes (never the home files): the identity door checks what only the
running gateway can check (the avatar catalog it loaded, the voices it offers, the downloaded models)
and records who changed what. The connection is the models CLI's: `--url`, else the gateway running
for this data dir; `--token`, else `$ABSTRACTGATEWAY_AUTH_TOKEN`, else this data dir's bootstrap admin
token for a loopback URL. Plain output; `--json` prints the route's payload.

    abstractgateway entity identity get castor [--rev N]
    abstractgateway entity identity set castor --style watch-lotus --color "#3366cc" \\
        --provider kokoro --model kokoro-82m --voice am_adam [--speed 1.1] [--quality-preset P]
    abstractgateway entity record castor [--track-record | --reputation | --acquaintances] [--since ISO]
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from typing import Any, Dict, List

from abstractruntime.identity.track_record import COMPLETION_RATE_ONLY

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2

# The record's basis, said for a person reading a terminal (ADR 0052 §5: "success" means the run
# completed until the assessment signal lands).
BASIS_SENTENCES = {
    COMPLETION_RATE_ONLY: (
        "Success here means the work finished (completion rate only): how good it was is not assessed yet."
    ),
}
NO_EXPERIENCE = "No experience yet"


def _connection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", default=None, help="Gateway base URL (default: $ABSTRACTGATEWAY_URL, else the gateway running for this data dir, else http://127.0.0.1:8080)")
    p.add_argument("--token", default=None, help="Bearer token (default: $ABSTRACTGATEWAY_AUTH_TOKEN, else this data dir's bootstrap admin token for a loopback URL)")
    p.add_argument("--data-dir", default=None, help="Gateway data dir used to find the running gateway and its token (default: same resolution as `serve`)")
    p.add_argument("--timeout-s", type=float, default=60.0, help="HTTP timeout per call (default: 60)")
    p.add_argument("--json", action="store_true", help="Print the route's JSON payload")


def add_identity_verbs(entity_sub: Any) -> None:
    """`entity identity get|set` and `entity record`, on the `entity` subcommand tree."""
    identity = entity_sub.add_parser("identity", help="An entity's digital identity: its look and its voice (through the gateway)")
    isub = identity.add_subparsers(dest="identity_cmd", required=True)
    get = isub.add_parser("get", help="Show the entity's identity (design, colour, voice, reference)")
    get.add_argument("name", help="Entity name or slug")
    get.add_argument("--rev", type=int, default=None, help="An earlier revision")
    _connection_args(get)
    put = isub.add_parser("set", help="Set the whole identity (an admin or the entity's creator)")
    put.add_argument("name", help="Entity name or slug")
    put.add_argument("--style", required=True, help="An avatar design id (for example watch-well; the ready designs are listed when an id is refused)")
    put.add_argument("--color", required=True, help="#rrggbb")
    put.add_argument("--provider", required=True, help="Voice provider")
    put.add_argument("--model", required=True, help="Voice model")
    put.add_argument("--voice", required=True, help="Voice id")
    put.add_argument("--speed", type=float, default=None)
    put.add_argument("--quality-preset", default=None)
    _connection_args(put)

    record = entity_sub.add_parser("record", help="An entity's track record, reputation or acquaintances (through the gateway)")
    record.add_argument("name", help="Entity name or slug")
    which = record.add_mutually_exclusive_group()
    which.add_argument("--track-record", dest="what", action="store_const", const="track-record", help="Sessions, runs, outcomes, tokens (the default)")
    which.add_argument("--reputation", dest="what", action="store_const", const="reputation", help="Trust per task kind, with its support")
    which.add_argument("--acquaintances", dest="what", action="store_const", const="acquaintances", help="The entities and people it met")
    record.add_argument("--since", default=None, help="Track record only: what started at or after this ISO-8601 time")
    _connection_args(record)


def _transport(args: argparse.Namespace) -> Any:
    from .models_engines_cli import _Http

    return _Http(args)


def _fail(args: argparse.Namespace, answer: Any, verb: str) -> int:
    from .models_engines_cli import _print_json

    if args.json and answer.data is not None:
        _print_json(answer.data)
    print(f"abstractgateway entity {verb}: {answer.message()}", file=sys.stderr)
    return EXIT_REFUSED if answer.status in (400, 401, 403, 404, 409) else EXIT_ERROR


def _quote(name: str) -> str:
    return urllib.parse.quote(str(name), safe="")


# --------------------------------------------------------------------------- identity


def identity_lines(payload: Dict[str, Any]) -> List[str]:
    identity = payload.get("identity") or {}
    avatar = identity.get("avatar") or {}
    voice = identity.get("voice")
    ref = payload.get("identity_ref") or {}
    lines = [f"{payload.get('entity_id')}  (identity {ref.get('of')}#{ref.get('rev')})"]
    lines.append(f"  Look:   {avatar.get('style')} in {avatar.get('color')}  ({avatar.get('source')}; Watch code {avatar.get('watch_code')})")
    if voice:
        lines.append(f"  Voice:  {voice.get('voice')}  ({voice.get('provider')} / {voice.get('model')}; {voice.get('source')})")
    else:
        lines.append("  Voice:  none yet (choose one, or set the Gateway default voice)")
    lines.append(f"  Set:    {identity.get('set_at')} by {identity.get('set_by')}")
    age = payload.get("age") or {}
    fingerprint = payload.get("fingerprint") or {}
    lines.append(f"  Born:   {age.get('born')} ({age.get('days')} days)   Fingerprint: {fingerprint.get('actor_id')}")
    return lines


def _identity_get(args: argparse.Namespace) -> int:
    from .models_engines_cli import _print_json

    path = f"/entities/{_quote(args.name)}/identity" + (f"?rev={int(args.rev)}" if args.rev is not None else "")
    answer = _transport(args).call("GET", path)
    if not answer.ok:
        return _fail(args, answer, "identity get")
    if args.json:
        _print_json(answer.data)
    else:
        print("\n".join(identity_lines(answer.data)))
    return EXIT_OK


def _identity_set(args: argparse.Namespace) -> int:
    from .models_engines_cli import _print_json

    voice: Dict[str, Any] = {"provider": args.provider, "model": args.model, "voice": args.voice}
    if args.speed is not None:
        voice["speed"] = args.speed
    if args.quality_preset:
        voice["quality_preset"] = args.quality_preset
    body = {"avatar": {"style": args.style, "color": args.color}, "voice": voice}
    answer = _transport(args).call("PUT", f"/entities/{_quote(args.name)}/identity", body)
    if not answer.ok:
        return _fail(args, answer, "identity set")
    if args.json:
        _print_json(answer.data)
    else:
        print("Saved.")
        print("\n".join(identity_lines(answer.data)))
    return EXIT_OK


# --------------------------------------------------------------------------- record


def _basis_line(basis: Any) -> str:
    return BASIS_SENTENCES.get(str(basis), f"Basis: {basis}.")


def track_record_lines(payload: Dict[str, Any]) -> List[str]:
    lines = [f"Track record of {payload.get('subject')}" + (f" since {payload['since']}" if payload.get("since") else "")]
    if not payload.get("runs"):
        lines.append(f"  {NO_EXPERIENCE}.")
    else:
        tokens = payload.get("tokens") or {}
        lines.append(
            f"  {payload.get('sessions')} sessions, {payload.get('runs')} runs: {payload.get('successes')} succeeded, "
            f"{payload.get('failures')} failed, {payload.get('cancelled')} cancelled, {payload.get('in_progress')} in progress"
        )
        lines.append(f"  Tokens: {tokens.get('total')} ({tokens.get('input')} in, {tokens.get('output')} out)")
        for kind, row in sorted((payload.get("by_task_kind") or {}).items()):
            lines.append(f"  {kind}: {row.get('runs')} runs, {row.get('successes')} succeeded, {row.get('failures')} failed")
    lines.append(f"  {_basis_line(payload.get('basis'))}")
    return lines


def _cell_text(cell: Dict[str, Any]) -> str:
    return NO_EXPERIENCE if not cell.get("support") else str(cell.get("text"))


def reputation_lines(payload: Dict[str, Any]) -> List[str]:
    overall = payload.get("overall") or {}
    lines = [f"Reputation of {payload.get('subject')}: {_cell_text(overall)}"]
    for kind, cell in sorted((payload.get("by_task_kind") or {}).items()):
        lines.append(f"  {kind}: {_cell_text(cell)}")
    evidence = payload.get("evidence") or []
    if evidence:
        lines.append("  Last outcomes:")
        for row in evidence[-5:]:
            lines.append(f"    {row.get('at')}  {row.get('outcome')}  {row.get('task_kind')}  ({row.get('signal')})")
    lines.append(f"  {_basis_line(payload.get('basis'))}")
    return lines


def acquaintances_lines(payload: Dict[str, Any]) -> List[str]:
    lines = [f"Acquaintances of {payload.get('subject')}"]
    for title, key in (("Entities", "entities"), ("People", "humans")):
        rows = payload.get(key) or []
        if not rows:
            lines.append(f"  {title}: none yet")
            continue
        lines.append(f"  {title}:")
        for row in rows:
            noun = "interaction" if row.get("interactions") == 1 else "interactions"
            lines.append(f"    {row.get('id')}: {row.get('interactions')} {noun}, last {row.get('last_seen')}")
    not_counted = payload.get("not_counted") or {}
    if not_counted:
        lines.append("  Not counted: " + "; ".join(f"{reason} ({n})" for reason, n in sorted(not_counted.items())))
    return lines


def _record(args: argparse.Namespace) -> int:
    from .models_engines_cli import _print_json

    what = args.what or "track-record"
    path = f"/entities/{_quote(args.name)}/{what}"
    if args.since:
        if what != "track-record":
            print("abstractgateway entity record: --since applies to the track record only", file=sys.stderr)
            return EXIT_REFUSED
        path += "?since=" + urllib.parse.quote(str(args.since), safe="")
    answer = _transport(args).call("GET", path)
    if not answer.ok:
        return _fail(args, answer, "record")
    if args.json:
        _print_json(answer.data)
        return EXIT_OK
    render = {"track-record": track_record_lines, "reputation": reputation_lines, "acquaintances": acquaintances_lines}[what]
    print("\n".join(render(answer.data)))
    return EXIT_OK


def run_identity_verbs(args: argparse.Namespace) -> int:
    if args.entity_cmd == "record":
        return _record(args)
    return {"get": _identity_get, "set": _identity_set}[args.identity_cmd](args)
