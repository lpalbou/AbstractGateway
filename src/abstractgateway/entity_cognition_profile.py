"""The entity's cognition profile through the gateway: `GET` and `PUT
/api/gateway/entities/{name}/cognition-profile`.

The profile is `<home>/cognition.yaml` (AbstractRuntime
`identity.cognition_profile` owns the format, the validation and the atomic
write): which model each part of its thinking uses (`summaries`,
`understanding`, `extraction`; each the mind unless chosen), how many
memories a turn brings to mind (`recall.shelf_size`), and how readily memory
work revisits what it understood (`rebuild.sensitivity`, `rebuild.cap_per_turn`).

The PUT follows the substrate door: one write at a time per entity, every
value checked before anything is recorded, the creator bound to the models
the gateway offers, a `cognition_profile_changed` marker in the entity's
history BEFORE the file moves (a marker that cannot be recorded refuses the
change with 503 and writes nothing). The routes in `routes/entities.py` are
thin wrappers over this module.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

# A running own-time loop reads the recall width when it starts; the GET says so.
RECALL_APPLIES_TO_RUNNING_LOOP = "on restart"

_PUT_BLOCKS = ("capabilities", "recall", "rebuild")


class CognitionProfileRefused(Exception):
    """A PUT the gateway refuses: `status` and `detail` become the HTTP answer."""

    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(detail if isinstance(detail, str) else str(detail))
        self.status = int(status)
        self.detail = detail


def _mind_of(home_dir: Path) -> Dict[str, Any]:
    """The mind its visits think with: `{provider, model, thinking, source}`;
    `source` is "entity", "gateway" or "unset" (then `note` says what to do)."""
    from .entity_chat import NO_TEXT_MODEL_REFUSAL, ChatOpenRefused, resolve_entity_mind

    try:
        mind = resolve_entity_mind(None, None, home_dir=home_dir)
    except ChatOpenRefused:
        return {"provider": None, "model": None, "thinking": None, "source": "unset", "note": NO_TEXT_MODEL_REFUSAL}
    return {
        "provider": mind["provider"],
        "model": mind["model"],
        "thinking": mind["thinking"],
        "source": mind["source"],
    }


def cognition_profile_view(home_dir: Path) -> Dict[str, Any]:
    """The GET answer:

        {"schema_version": 1,
         "capabilities": {cap: {"choice": {"inherit": "mind"} | {provider, model, thinking, max_input_tokens},
                                "effective": {provider, model, thinking} | null,
                                "source": "profile" | "mind"}},
         "recall": {"shelf_size": int, "source": "profile" | "default", "applies_to_running_loop": "on restart"},
         "rebuild": {"sensitivity": float, "cap_per_turn": int, "source": "profile" | "default",
                     "sources": {"sensitivity": ..., "cap_per_turn": ...}},
         "mind": {provider, model, thinking, "source": "entity" | "gateway" | "unset"[, "note"]}}

    `effective` is null only for a capability that inherits a mind the gateway
    cannot name (no text model at all; `mind.note` says what to do)."""
    from abstractruntime.identity.cognition_profile import CAPABILITIES, read_cognition_profile, resolve_capability

    from .entity_chat import DEFAULT_ENTITY_CHAT_SHELF_SIZE

    home_dir = Path(home_dir)
    profile = read_cognition_profile(home_dir)
    mind = _mind_of(home_dir)
    mind_known = mind["source"] != "unset"
    capabilities: Dict[str, Any] = {}
    for cap in CAPABILITIES:
        choice = dict(profile["capabilities"][cap]["choice"])
        if "inherit" in choice and not mind_known:
            capabilities[cap] = {"choice": choice, "effective": None, "source": "mind"}
            continue
        resolved = resolve_capability(home_dir, cap, mind=mind if mind_known else None)
        capabilities[cap] = {
            "choice": choice,
            "effective": {
                "provider": resolved["provider"],
                "model": resolved["model"],
                "thinking": resolved["thinking"],
            },
            "source": resolved["source"],
        }
    recall = profile["recall"]
    rebuild = profile["rebuild"]
    shelf = recall["shelf_size"]
    return {
        "schema_version": profile["schema_version"],
        "capabilities": capabilities,
        "recall": {
            "shelf_size": int(shelf) if shelf is not None else int(DEFAULT_ENTITY_CHAT_SHELF_SIZE),
            "source": recall["source"],
            "applies_to_running_loop": RECALL_APPLIES_TO_RUNNING_LOOP,
        },
        "rebuild": {
            "sensitivity": rebuild["sensitivity"],
            "cap_per_turn": rebuild["cap_per_turn"],
            "source": "profile" if "profile" in rebuild["sources"].values() else "default",
            "sources": dict(rebuild["sources"]),
        },
        "mind": mind,
    }


def _invalid(e: Exception) -> CognitionProfileRefused:
    return CognitionProfileRefused(400, getattr(e, "sentence", None) or str(e))


def _check_endpoint(label: str, provider: str, *, data_dir: Path, root_data_dir: Path) -> None:
    """An `endpoint:` provider must name an enabled provider connection of this gateway."""
    if not provider.lower().startswith("endpoint:"):
        return
    from .provider_endpoint_profiles import ProviderEndpointProfileError, resolve_effective_endpoint_profile

    try:
        profile = resolve_effective_endpoint_profile(provider, base_dir=data_dir, root_base_dir=root_data_dir)
    except ProviderEndpointProfileError as e:
        raise CognitionProfileRefused(400, f"{label} can't use {provider}: {e}")
    if profile is None:
        raise CognitionProfileRefused(
            400,
            f"{label} can't use {provider}: it isn't an enabled provider connection on this gateway. "
            "Choose another provider, or the same model as the mind.",
        )


def plan_cognition_profile_put(
    home_dir: Path,
    body: Any,
    *,
    role: Optional[str],
    data_dir: Path,
    root_data_dir: Path,
    bound_problem: Callable[[str, str], Optional[str]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Check a PUT body against the stored profile; return `(old, new)` stored forms.

    Body: any subset of `{capabilities: {cap: {"inherit": "mind"} | {provider, model, thinking?,
    max_input_tokens?} | null}, recall: {shelf_size: int | null}, rebuild: {sensitivity: number |
    null, cap_per_turn: int | null}}`. A field that is absent keeps its value; null returns it to its
    default (for a capability: the mind). A capability sent unchanged is kept as it is; a changed one
    must name an enabled `endpoint:` connection when it uses one, and, for the entity's creator
    (`role == "creator"`), a model the gateway offers (`bound_problem` returns the sentence, else
    None). Raises `CognitionProfileRefused` (400 for a value outside the schema, 403 for the bound)
    before anything is written or recorded."""
    from abstractruntime.identity.cognition_profile import (
        CAPABILITY_LABELS,
        CognitionProfileInvalid,
        check_capability_name,
        stored_cognition_profile,
        validate_cap_per_turn,
        validate_capability_choice,
        validate_cognition_profile,
        validate_sensitivity,
        validate_shelf_size,
    )

    from .entity_settings_access import REASON_NOT_OFFERED

    if not isinstance(body, Mapping):
        raise CognitionProfileRefused(400, "Send an object with capabilities, recall or rebuild.")
    unknown = [str(k) for k in body.keys() if k not in _PUT_BLOCKS]
    if unknown:
        raise CognitionProfileRefused(400, f"Unknown field {unknown[0]!r}: send capabilities, recall or rebuild.")
    old = stored_cognition_profile(Path(home_dir))
    new = copy.deepcopy(old)
    try:
        if "capabilities" in body:
            caps_in = body["capabilities"]
            if not isinstance(caps_in, Mapping):
                raise CognitionProfileRefused(
                    400, 'capabilities must be an object, for example {"summaries": {"inherit": "mind"}}.'
                )
            caps: Dict[str, Any] = dict(new.get("capabilities") or {})
            for cap_in, choice_in in caps_in.items():
                cap = check_capability_name(cap_in)
                label = CAPABILITY_LABELS[cap]
                if choice_in is None:
                    caps.pop(cap, None)
                    continue
                choice = validate_capability_choice(cap, choice_in)
                if "inherit" in choice:
                    caps.pop(cap, None)
                    continue
                if caps.get(cap) == choice:
                    continue  # unchanged: kept as it is
                _check_endpoint(label, choice["provider"], data_dir=data_dir, root_data_dir=root_data_dir)
                if role == "creator":
                    problem = bound_problem(choice["provider"], choice["model"])
                    if problem:
                        raise CognitionProfileRefused(
                            403, {"reason_code": REASON_NOT_OFFERED, "message": f"{label}: {problem}"}
                        )
                caps[cap] = choice
            if caps:
                new["capabilities"] = caps
            else:
                new.pop("capabilities", None)
        for block, checks in (
            ("recall", {"shelf_size": validate_shelf_size}),
            ("rebuild", {"sensitivity": validate_sensitivity, "cap_per_turn": validate_cap_per_turn}),
        ):
            if block not in body:
                continue
            raw = body[block]
            example = '{"shelf_size": 50}' if block == "recall" else '{"sensitivity": 0.5, "cap_per_turn": 3}'
            if not isinstance(raw, Mapping):
                raise CognitionProfileRefused(400, f"{block} must be an object, for example {example}.")
            extra = [str(k) for k in raw.keys() if k not in checks]
            if extra:
                raise CognitionProfileRefused(
                    400, f"{block} has an unknown field {extra[0]!r}. Allowed: {', '.join(checks)}."
                )
            values: Dict[str, Any] = dict(new.get(block) or {})
            for key, check in checks.items():
                if key not in raw:
                    continue
                if raw[key] is None:
                    values.pop(key, None)
                else:
                    values[key] = check(raw[key])
            if values:
                new[block] = values
            else:
                new.pop(block, None)
        new = validate_cognition_profile(new)
    except CognitionProfileInvalid as e:
        raise _invalid(e)
    return old, new


def put_cognition_profile(
    *,
    home_dir: Path,
    body: Any,
    role: Optional[str],
    actor: str,
    data_dir: Path,
    root_data_dir: Path,
    bound_problem: Callable[[str, str], Optional[str]],
    record_marker: Callable[[Dict[str, Any]], None],
) -> Dict[str, Any]:
    """Check, record, write; return the GET answer. The caller holds the entity's write lock.

    `record_marker(details)` records the `cognition_profile_changed` host marker and raises when it
    cannot; then nothing is written (503). A PUT that changes nothing records nothing."""
    from abstractruntime.identity.cognition_profile import write_cognition_profile

    old, new = plan_cognition_profile_put(
        home_dir, body, role=role, data_dir=data_dir, root_data_dir=root_data_dir, bound_problem=bound_problem
    )
    if new != old:
        try:
            record_marker({"channel": "operator", "by": actor, "as": role, "old": old, "new": new})
        except Exception as e:  # noqa: BLE001
            raise CognitionProfileRefused(
                503,
                f"Thinking settings not changed: the change could not be recorded in its history ({e}). Try again.",
            )
        write_cognition_profile(Path(home_dir), new)
    return cognition_profile_view(Path(home_dir))
