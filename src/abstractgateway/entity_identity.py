"""An entity's digital identity at the door: its look and its voice (framework ADR 0052, backlog 1119).

The record is AbstractRuntime's (`abstractruntime.identity.digital_identity`: `<home>/identity.json`,
its bounds, its rev, its `identity_changed` biography row). The gateway checks what only it can
check, then calls the runtime writer:

- the refusal ladder at SET time, in this order, for every role (ADR section 8): the home is held for
  maintenance (409); the design is not in the avatar catalog loaded on this gateway, or cannot be
  drawn on the Watch (400, naming the catalog); the voice is not one this gateway offers (400, the
  offered list); the voice's model is not downloaded (409, with the download action). After the set
  the entity lane never substitutes a voice.
- the recorded default (section 2): written once per home, at birth, by the boot pass over every
  existing home (idempotent, a held home skipped and logged, a record never overwritten) and by
  `GET /identity` for a home the boot pass skipped. No default without a verifiable design: a gateway
  that cannot load the avatar catalog writes nothing.
- the host marker `identity_changed` (rev, from, to, by), recorded inside the runtime writer's check,
  so a marker that cannot be written refuses the change before the biography row and the file.
- the view `GET /identity` serves: the record with the design's `watch_code`, the fingerprint and
  the age.

No flags and no environment variables.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pydantic import BaseModel, Field

from abstractruntime.identity.digital_identity import (
    AvatarChoice,
    DigitalIdentity,
    IdentityInvalid,
    IdentityRef,
    IdentityUnreadable,
    VoiceChoice,
    default_identity,
    identity_at_rev,
    identity_fingerprint,
    identity_ref,
    read_identity,
    record_default_identity,
    write_identity,
)

logger = logging.getLogger("abstractgateway.entity_identity")

__all__ = [
    "IdentityRefused",
    "avatar_catalog",
    "boot_pass",
    "check_choice",
    "derived_shape",
    "entity_identity_ref",
    "gateway_default_voice",
    "identity_view",
    "list_row_identity",
    "materialize_default_identity",
    "no_voice_sentence",
    "set_identity",
    "style_for_shape",
]

# Which door or pass wrote a record (the biography row's `provenance`).
PROVENANCE_BIRTH = "gateway:birth"
PROVENANCE_BOOT = "gateway:boot-pass"
PROVENANCE_LAZY = "gateway:get-identity"
PROVENANCE_IDENTITY = "gateway:put-identity"
PROVENANCE_VOICE = "gateway:put-voice"
PROVENANCE_APPEARANCE = "gateway:put-appearance"

# Legacy `appearance.json` shapes and the designs that draw them (abstractavatar `watch-shape-*`).
_SHAPE_PREFIX = "watch-shape-"
_PLANET = "planet"

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class IdentityAvatarBody(BaseModel):
    style: str = Field(..., description="An abstractavatar design id this gateway's catalog draws on the Watch")
    color: str = Field(..., description="#rrggbb")


class IdentityVoiceBody(BaseModel):
    provider: str
    model: str
    voice: str
    speed: Optional[float] = Field(default=None, gt=0, le=4)
    quality_preset: Optional[str] = Field(default=None, max_length=32)


class IdentityBody(BaseModel):
    """The body that names an identity: `PUT /entities/{name}/identity`, `identity` on
    `POST /entities` and on `POST /runs/start`."""

    avatar: IdentityAvatarBody
    voice: Optional[IdentityVoiceBody] = Field(default=None, description="Required: an identity someone chooses always names its voice")


def choice_from_body(avatar: Any, voice: Any) -> Tuple[AvatarChoice, Optional[VoiceChoice]]:
    """The chosen avatar and voice a body names (`source: chosen`); bounds are the runtime's."""
    chosen_avatar = AvatarChoice(style=str(avatar.style).strip(), color=str(avatar.color).strip().lower(), source="chosen")
    chosen_voice = None
    if voice is not None:
        chosen_voice = VoiceChoice(
            provider=str(voice.provider).strip(),
            model=str(voice.model).strip(),
            voice=str(voice.voice).strip(),
            source="chosen",
            speed=float(voice.speed) if voice.speed is not None else None,
            quality_preset=(str(voice.quality_preset).strip() or None) if voice.quality_preset is not None else None,
        )
    return chosen_avatar, chosen_voice


def identity_refusal_body(e: Exception) -> Tuple[int, Dict[str, Any]]:
    """(status, JSON body) of a refused or invalid identity, one shape for every door."""
    if isinstance(e, IdentityRefused):
        content: Dict[str, Any] = {"detail": e.detail}
        if e.error:
            content["error"] = e.error
        return e.status, content
    if isinstance(e, IdentityInvalid):
        return 400, {"detail": str(e), "field": e.field}
    if isinstance(e, IdentityUnreadable):
        return 409, {"detail": f"The identity record can't be read, so it is not changed or served: {e}"}
    raise e


class IdentityRefused(Exception):
    """A set the door refuses: `status` and `detail` are the HTTP answer (a sentence, or
    `{detail, error}` for the download action)."""

    def __init__(self, status: int, detail: Any, *, error: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(detail if isinstance(detail, str) else str(detail))
        self.status = int(status)
        self.detail = detail
        self.error = error


def _home_lock(home_dir: Path) -> threading.Lock:
    key = str(Path(home_dir).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


# --------------------------------------------------------------------------- what the gateway knows


_CATALOG: Optional[Dict[str, Dict[str, Any]]] = None


def avatar_catalog() -> Dict[str, Dict[str, Any]]:
    """The avatar designs this gateway loaded (`abstractavatar.catalog()`), by id, read once per
    process. Raises `IdentityRefused(503)` when the package cannot be loaded: no design is verifiable
    then (a failure is never cached: installing the package fixes it at the next call)."""
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    try:
        import abstractavatar
    except ImportError as e:
        raise IdentityRefused(
            503,
            f"The avatar catalog is not available on this gateway (abstractavatar could not be loaded: {e}); "
            "no identity can be recorded until it is installed.",
        ) from e
    _CATALOG = {str(row["id"]): dict(row) for row in abstractavatar.catalog()}
    return _CATALOG


def gateway_default_voice(data_dir: Path) -> Optional[Dict[str, Any]]:
    """The gateway's default voice for entities of the runtime at `data_dir`: the `output.voice`
    capability route as `{provider, model, voice}`, or None when it does not name all three (the
    recorded default then has no voice, and says so)."""
    from .core_config import gateway_capability_defaults_payload

    payload = gateway_capability_defaults_payload(base_dir=Path(data_dir))
    for row in payload.get("routes") or []:
        if str(row.get("kind")) == "output" and str(row.get("modality")) == "voice" and row.get("provider"):
            options = row.get("options") if isinstance(row.get("options"), dict) else {}
            triple = {
                "provider": str(row.get("provider") or "").strip(),
                "model": str(row.get("model") or "").strip(),
                "voice": str(options.get("voice") or "").strip(),
            }
            return triple if all(triple.values()) else None
    return None


def no_voice_sentence(name: str) -> str:
    """The recorded absence of a voice (ADR 0052 section 8), the entity's name in place."""
    return f"{name} has no voice yet: choose one in its Identity settings or set the Gateway default voice"


def _display_name(manifest: Any) -> str:
    return str(getattr(manifest, "name", "") or getattr(manifest, "slug", "") or "This entity")


def _held(registry: Any, slug: str) -> bool:
    return bool(registry.maintenance_hold_status(slug).get("held"))


def _held_sentence(manifest: Any) -> str:
    return (
        f"{_display_name(manifest)} is closed for maintenance: its identity can't change until the "
        "maintenance window closes."
    )


# --------------------------------------------------------------------------- the ladder


def _check_style(style: str, catalog: Mapping[str, Mapping[str, Any]]) -> None:
    row = catalog.get(style)
    drawable = sorted(k for k, v in catalog.items() if str(v.get("watch_code") or "").strip())
    if row is None:
        raise IdentityRefused(
            400,
            f"{style} isn't an avatar design this gateway has. Designs: {', '.join(drawable)}.",
        )
    if not str(row.get("watch_code") or "").strip():
        raise IdentityRefused(
            400,
            f"{style} can't be drawn on the Watch, so it can't be an entity's look. Designs: {', '.join(drawable)}.",
        )


def _check_voice_offered(voice: VoiceChoice) -> None:
    from .entity_settings_access import voice_bound_problem

    problem = voice_bound_problem(voice.provider, voice.model, voice.voice)
    if problem:
        raise IdentityRefused(400, problem)


def _check_voice_downloaded(voice: VoiceChoice) -> None:
    from .core_config import model_not_downloaded

    missing = model_not_downloaded(voice.provider, voice.model)
    if missing:
        raise IdentityRefused(409, str(missing.get("message") or ""), error=missing)


def check_choice(name: str, avatar: AvatarChoice, voice: Optional[VoiceChoice]) -> None:
    """The ladder after the hold: the design, then the voice offered, then its model downloaded.
    Also what `POST /entities` runs before a home exists."""
    _check_style(avatar.style, avatar_catalog())
    if voice is None:
        raise IdentityRefused(400, f"{name}'s identity needs a voice: an identity someone chooses always names one.")
    _check_voice_offered(voice)
    _check_voice_downloaded(voice)


def _ladder(registry: Any, manifest: Any, avatar: AvatarChoice, voice: Optional[VoiceChoice]) -> None:
    """The refusal ladder for a chosen identity, in the ADR's order; nothing is written on a refusal."""
    if _held(registry, manifest.slug):
        raise IdentityRefused(409, _held_sentence(manifest))
    check_choice(_display_name(manifest), avatar, voice)


# --------------------------------------------------------------------------- writing


def _record_marker(registry: Any, manifest: Any, previous: Optional[DigitalIdentity], record: DigitalIdentity, *, by: str, role: Optional[str], door: str) -> None:
    """The host marker `identity_changed`, recorded inside the runtime writer's check: when it
    cannot be written, the change is refused before the biography row and the file."""
    from .entity_replay import record_host_marker

    home = registry.get_home(manifest.slug)
    record_host_marker(
        entities_dir=registry.entities_dir,
        slug=manifest.slug,
        entity_id=manifest.entity_id,
        kind="identity_changed",
        journal_seq=int(home.memory.current_seq()),
        details={
            "channel": "operator",
            "by": by,
            "as": role,
            "door": door,
            "rev": record.rev,
            "from": previous.to_dict() if previous is not None else None,
            "to": record.to_dict(),
        },
    )


def _set_by(actor: str) -> str:
    """`set_by` of a chosen record: the principal as `namespace:name` (`person:laurent`), `operator`
    when the door has no principal."""
    text = str(actor or "").strip()
    return text if text.count(":") == 1 and all(text.split(":")) else "operator"


def set_identity(
    registry: Any,
    manifest: Any,
    *,
    avatar: AvatarChoice,
    voice: Optional[VoiceChoice],
    actor: str,
    role: Optional[str],
    provenance: str,
) -> DigitalIdentity:
    """Validate (the ladder) and record a chosen identity: `rev + 1`, the biography row, the host
    marker, the file. Raises `IdentityRefused` with the answer, or `IdentityInvalid` naming a field."""
    home_dir = registry.entities_dir / manifest.slug
    with _home_lock(home_dir):
        _ladder(registry, manifest, avatar, voice)
        previous = read_identity(home_dir)

        def check(record: DigitalIdentity) -> None:
            _record_marker(registry, manifest, previous, record, by=actor, role=role, door=provenance)

        return write_identity(
            home_dir,
            avatar=avatar,
            voice=voice,
            set_by=_set_by(actor),
            cause="chosen",
            provenance=provenance,
            check=check,
        )


def materialize_default_identity(
    entities_dir: Path, slug: str, *, entity_id: str, data_dir: Path, held: bool, provenance: str
) -> Tuple[Optional[DigitalIdentity], str]:
    """Write the recorded default for one home once (ADR 0052 section 2). Returns (record, outcome):
    `written`, `kept` (it already had one), `held` (skipped: the home is closed for maintenance),
    `no_catalog` (skipped: no design is verifiable without the avatar catalog)."""
    home_dir = Path(entities_dir) / slug
    with _home_lock(home_dir):
        existing = read_identity(home_dir)
        if existing is not None:
            return existing, "kept"
        if held:
            return None, "held"
        try:
            catalog = avatar_catalog()
        except IdentityRefused:
            return None, "no_catalog"

        def check(record: DigitalIdentity) -> None:
            _check_style(record.avatar.style, catalog)

        record, written = record_default_identity(
            home_dir,
            entity_id=entity_id,
            gateway_default_voice=gateway_default_voice(data_dir),
            provenance=provenance,
            check=check,
        )
        return record, ("written" if written else "kept")


def _manifest_doc(home_dir: Path) -> Optional[Dict[str, Any]]:
    import json

    try:
        doc = json.loads((home_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def boot_pass(entities_dirs: List[Path]) -> Dict[str, int]:
    """The boot pass (ADR 0052 section 2 b): every existing home without a record gets the recorded
    default; a held home is skipped; a record is never overwritten. One log line per home written or
    skipped. Returns the counts by outcome."""
    from .entities import MAINTENANCE_HOLD_FILENAME

    counts: Dict[str, int] = {}
    for entities_dir in entities_dirs:
        entities_dir = Path(entities_dir)
        if not entities_dir.is_dir():
            continue
        for home_dir in sorted(p for p in entities_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            doc = _manifest_doc(home_dir)
            if doc is None or not str(doc.get("entity_id") or "").strip():
                continue
            slug = home_dir.name
            try:
                _record, outcome = materialize_default_identity(
                    entities_dir,
                    slug,
                    entity_id=str(doc["entity_id"]),
                    data_dir=entities_dir.parent,
                    held=(home_dir / MAINTENANCE_HOLD_FILENAME).exists(),
                    provenance=PROVENANCE_BOOT,
                )
            except (IdentityUnreadable, IdentityInvalid, OSError, ValueError) as e:
                outcome = "error"
                logger.error("identity: %s: the recorded default could not be written: %s", home_dir, e)
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == "written":
                logger.info("identity: %s: the recorded default identity was written (rev 1)", home_dir)
            elif outcome == "held":
                logger.info("identity: %s: closed for maintenance; its default identity is written at the next boot or read", home_dir)
            elif outcome == "no_catalog":
                logger.error("identity: %s: no default identity written: the avatar catalog (abstractavatar) is not available", home_dir)
    return counts


# --------------------------------------------------------------------------- reading


def entity_identity_ref(entity_id: str, home_dir: Path) -> Optional[IdentityRef]:
    """The reference to the home's current identity (`entity:castor#3`), or None when the home has
    no record yet (a ref is never fabricated)."""
    record = read_identity(Path(home_dir))
    if record is None:
        return None
    return identity_ref(str(entity_id), record.rev)


def _age(created_at: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    text = str(created_at or "")
    try:
        born = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return {"born": text or None, "days": None}
    if born.tzinfo is None:
        born = born.replace(tzinfo=timezone.utc)
    seconds = ((now or datetime.now(timezone.utc)) - born).total_seconds()
    return {"born": text, "days": max(0, int(math.floor(seconds / 86400.0)))}


def _record_payload(record: DigitalIdentity, catalog: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    avatar = record.avatar.to_dict()
    row = catalog.get(record.avatar.style) or {}
    avatar["watch_code"] = str(row.get("watch_code") or "") or None
    return {
        "rev": record.rev,
        "avatar": avatar,
        "voice": record.voice.to_dict() if record.voice is not None else None,
        "set_at": record.set_at,
        "set_by": record.set_by,
    }


def identity_view(registry: Any, manifest: Any, *, rev: Optional[int] = None) -> Dict[str, Any]:
    """`GET /entities/{name}/identity[?rev=N]`: `{entity_id, identity, fingerprint, age}`. A home the
    boot pass skipped records its default here (unless held). Raises `IdentityRefused` (409 held,
    404 unknown rev, 503 no catalog) and `IdentityUnreadable`."""
    home_dir = registry.entities_dir / manifest.slug
    if _held(registry, manifest.slug):
        raise IdentityRefused(409, f"{_display_name(manifest)} is closed for maintenance; its identity is served again when the window closes.")
    catalog = avatar_catalog()
    if rev is None:
        record, outcome = materialize_default_identity(
            registry.entities_dir,
            manifest.slug,
            entity_id=manifest.entity_id,
            data_dir=registry.data_dir,
            held=False,
            provenance=PROVENANCE_LAZY,
        )
        if record is None:
            raise IdentityRefused(503, f"{_display_name(manifest)} has no identity yet ({outcome}).")
    else:
        try:
            record = identity_at_rev(home_dir, int(rev))
        except LookupError as e:
            raise IdentityRefused(404, str(e)) from e
    home = registry.get_home(manifest.slug)
    fingerprint = identity_fingerprint(manifest.to_dict(), book_head=home.diary.head())
    return {
        "entity_id": manifest.entity_id,
        "identity": _record_payload(record, catalog),
        "identity_ref": identity_ref(manifest.entity_id, record.rev).to_dict(),
        "fingerprint": fingerprint.to_dict(),
        "age": _age(manifest.created_at),
    }


def list_row_identity(registry: Any, manifest: Any) -> Optional[Dict[str, Any]]:
    """`identity` on the `GET /entities` row: the current record with its `watch_code`, or None (a
    legacy home before the boot pass, a held home, an unreadable record, no catalog). Never writes."""
    if _held(registry, manifest.slug):
        return None
    try:
        record = read_identity(registry.entities_dir / manifest.slug)
        if record is None:
            return None
        return _record_payload(record, avatar_catalog())
    except (IdentityUnreadable, IdentityRefused) as e:
        logger.warning("identity: %s: not on the list row: %s", manifest.slug, e)
        return None


# --------------------------------------------------------------------------- the legacy files' view


def derived_shape(style: str) -> str:
    """The legacy appearance shape of a design: `watch-shape-<shape>` draws that shape, every other
    design is the orb (`planet`)."""
    return style[len(_SHAPE_PREFIX):] if style.startswith(_SHAPE_PREFIX) else _PLANET


def style_for_shape(shape: str, current_style: str, entity_id: str) -> str:
    """The design a legacy `PUT /appearance` shape writes: `kitty|face|wave` -> `watch-shape-<shape>`;
    `planet` keeps an orb design, and replaces a shape design by the entity's recorded-default design
    (a deterministic orb, never a guess)."""
    if shape != _PLANET:
        return f"{_SHAPE_PREFIX}{shape}"
    if not current_style.startswith(_SHAPE_PREFIX):
        return current_style
    avatar, _voice = default_identity(entity_id, None)
    return avatar.style
