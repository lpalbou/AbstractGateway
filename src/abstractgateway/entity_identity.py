"""An entity's digital identity at the door: its look and its voice (framework ADR 0052, backlog 1119).

The record is AbstractRuntime's (`abstractruntime.identity.digital_identity`: `<home>/identity.json`,
its bounds, its rev, its `identity_changed` biography row). The gateway checks what only it can
check, then calls the runtime writer:

- the refusal ladder at SET time, in this order, for every role (ADR section 8): the home is held for
  maintenance (409); the design is not one the avatar catalog loaded on this gateway knows (active
  or retired), or cannot be drawn on the Watch (400, listing the ready designs only); the voice is not one this gateway offers (400, the
  offered list); the voice's model is not downloaded (409, with the download action). After the set
  the entity lane never substitutes a voice.
- the recorded default (section 2): written once per home, at birth, by the boot pass over every
  existing home (idempotent, a held home skipped and logged, a record never overwritten) and by
  `GET /identity` for a home the boot pass skipped. No default without a verifiable design: a gateway
  that cannot load the avatar catalog writes nothing.
- the host marker `identity_changed` (rev, from, to, by), recorded inside the runtime writer's check,
  so a marker that cannot be written refuses the change before the biography row and the file.
- the view `GET /identity` serves: the record with the design's `watch_code`, the fingerprint and
  the age. A stored design the catalog retired is served as its replacement (`style`), the stored id
  beside it (`style_requested`): never refused, never drawn blank. The record keeps the id it was
  given, so a design that returns under the same id comes back by itself.
- what the gateway offers or assigns as a look comes from the catalog's ready designs only;
  experimental designs stay reachable by their id.

No flags and no environment variables.
"""

from __future__ import annotations

import logging
import math
import os
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
    "ready_designs",
    "resolve_style",
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

# Legacy `appearance.json` shapes. Their designs (abstractavatar `watch-shape-<shape>`) are retired:
# a shape writes what the catalog's retired map draws in their place (`resolve_style`).
_SHAPE_PREFIX = "watch-shape-"
_SHAPES = ("kitty", "face", "wave")
_PLANET = "planet"
_READY = "ready"
_RETIRED = "retired"

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


def without_paths(message: str, *roots: Any) -> str:
    """`message` with the given directories removed from the front of the paths it names
    (`/data/entities/castor/identity.json` -> `castor/identity.json`): an answer names the file, the
    gateway log keeps the full path. Exact prefixes the caller knows, never a guess."""
    out = str(message)
    for root in roots:
        if root:
            out = out.replace(str(Path(root)) + os.sep, "")
    return out


def identity_refusal_body(e: Exception, *, root: Any = None) -> Tuple[int, Dict[str, Any]]:
    """(status, JSON body) of a refused or invalid identity, one shape for every door. An unreadable
    record names its file relative to `root` (the entities folder); the full path goes to the log."""
    if isinstance(e, IdentityRefused):
        content: Dict[str, Any] = {"detail": e.detail}
        if e.error:
            content["error"] = e.error
        return e.status, content
    if isinstance(e, IdentityInvalid):
        return 400, {"detail": str(e), "field": e.field}
    if isinstance(e, IdentityUnreadable):
        logger.error("identity: unreadable record: %s", e)
        return 409, {"detail": f"The identity record can't be read, so it is not changed or served: {without_paths(str(e), root)}"}
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
    """Every avatar design id this gateway knows, by id, read once per process: the active designs
    (`abstractavatar.catalog()`, `status` `ready` or `experimental`, catalog order; the catalog's
    default design carries `default: True`), then the retired ones (`abstractavatar.retired()`,
    `status: retired`, `replacement`: the active design drawn in its place, as
    `abstractavatar.resolve_style` answers). Raises `IdentityRefused(503)` when the package cannot be
    loaded: no design is verifiable then (a failure is never cached: installing the package fixes it
    at the next call)."""
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
    if not all(hasattr(abstractavatar, name) for name in ("retired", "resolve_style")):
        raise IdentityRefused(
            503,
            f"The avatar catalog on this gateway is too old (abstractavatar {getattr(abstractavatar, '__version__', '?')}: "
            "it has no retired designs); install abstractavatar 0.2.0 or later.",
        )
    try:
        catalog: Dict[str, Dict[str, Any]] = {str(row["id"]): dict(row) for row in abstractavatar.catalog()}
        default = abstractavatar.resolve_style("")  # an id the catalog does not have draws its default
        catalog[default]["default"] = True
        for row in abstractavatar.retired():
            key = str(row["id"])
            catalog[key] = {**dict(row), "status": _RETIRED, "replacement": abstractavatar.resolve_style(key)}
    except (RuntimeError, OSError, KeyError) as e:  # the native library is missing or refuses its ABI
        raise IdentityRefused(
            503,
            f"The avatar catalog is not available on this gateway (abstractavatar could not read it: {e}); "
            "no identity can be recorded until it is repaired.",
        ) from e
    _CATALOG = catalog
    return _CATALOG


def resolve_style(style: str, catalog: Mapping[str, Mapping[str, Any]]) -> str:
    """The active design drawn for a design id: the id itself when it is active, a retired id's
    replacement, any other id the catalog's default (the rule of `abstractavatar.resolve_style`,
    over the catalog this gateway loaded)."""
    row = catalog.get(style)
    if row is not None:
        return str(row["replacement"]) if row.get("status") == _RETIRED else style
    default = next((key for key, value in catalog.items() if value.get("default")), None)
    if default is None:
        raise IdentityRefused(503, "The avatar catalog on this gateway names no default design, so an unknown design can't be drawn.")
    return default


def _drawable(row: Mapping[str, Any]) -> bool:
    return bool(str(row.get("watch_code") or "").strip())


def ready_designs(catalog: Optional[Mapping[str, Mapping[str, Any]]] = None) -> List[str]:
    """The looks this gateway offers: the catalog's ready designs that the Watch draws, in catalog
    order. Never an experimental design (reachable by its id only) nor a retired one."""
    rows = avatar_catalog() if catalog is None else catalog
    return [key for key, row in rows.items() if row.get("status") == _READY and _drawable(row)]


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
    """Any id the catalog knows is accepted, active or retired (a retired id draws its replacement);
    the refusals list the ready designs only."""
    offered = ", ".join(ready_designs(catalog))
    if style not in catalog:
        raise IdentityRefused(
            400,
            f"{style} isn't an avatar design this gateway has. Designs: {offered}.",
        )
    if not _drawable(catalog[resolve_style(style, catalog)]):
        raise IdentityRefused(
            400,
            f"{style} can't be drawn on the Watch, so it can't be an entity's look. Designs: {offered}.",
        )


#: Whose voices a conversation may speak with (said in its refusal of any other voice): the
#: conversation is the caller's own, heard in their plane.
CONVERSATION_VOICES_SENTENCE = "A conversation speaks with this gateway's shared voices and your own voices."


def _check_voice_offered(voice: VoiceChoice, name: str, *, request: Any = None, for_conversation: bool = False,
                         offered: Optional[Tuple[list, list, Optional[str]]] = None) -> None:
    """The voice is one this gateway offers THE CALLER (`GET /voice/voices`, what the caller's
    voice picker lists: `entity_settings_access.offered_voices`). The sentence is the identity
    page's: what is missing, and what to do; a voice the caller's list lacks (another account's
    own voice, an unknown id) is refused with whose voices it may be
    (`entity_settings_access.ENTITY_VOICES_SENTENCE`)."""
    from .entity_settings_access import ENTITY_VOICES_SENTENCE, _listed, offered_voices

    voices, models, err = offered if offered is not None else offered_voices(voice.provider, voice.model, request)
    if not voices:
        why = f" ({err})" if err else ""
        raise IdentityRefused(
            400, f"{name} can't speak with {voice.voice}: this gateway offers no {voice.provider} voice right now{why}. Choose a voice it offers."
        )
    if not any(v.lower() == voice.voice.lower() for v in voices):
        whose = CONVERSATION_VOICES_SENTENCE if for_conversation else ENTITY_VOICES_SENTENCE
        raise IdentityRefused(
            400,
            f"{name} can't speak with {voice.voice}: it isn't a {voice.provider} voice {name} can use. {whose} "
            f"Choose one of: {_listed(voices)}.",
        )
    if models and not any(m.lower() == voice.model.lower() for m in models):
        raise IdentityRefused(
            400, f"{name} can't speak with {voice.model}: it isn't a {voice.provider} speech model this gateway offers. Choose one of: {_listed(models)}."
        )


def _check_voice_downloaded(voice: VoiceChoice, name: str) -> None:
    """The voice's model is on this computer (the typed answer of `core_config.model_not_downloaded`,
    never its sentence): refused with what is missing and how to get it."""
    from .core_config import model_not_downloaded

    missing = model_not_downloaded(voice.provider, voice.model)
    if missing:
        artifact = missing.get("artifact") or voice.model
        size = f" ({missing['size_mb']} MB)" if missing.get("size_mb") else ""
        how = f"the Models page, or: {missing['command']}" if missing.get("command") else "the Models page"
        raise IdentityRefused(
            409,
            f"{name}'s voice needs the {voice.provider} model {artifact}{size}, which is not downloaded on this computer yet. "
            f"Download it ({how}), then save the identity again.",
            error=missing,
        )


def check_choice(name: str, avatar: AvatarChoice, voice: Optional[VoiceChoice], *, request: Any = None,
                 for_conversation: bool = False, offered: Optional[Tuple[list, list, Optional[str]]] = None) -> None:
    """The ladder after the hold: the design, then the voice offered (to the caller), then its
    model downloaded. Also what `POST /entities` runs before a home exists, and a conversation's
    identity (`for_conversation`). Blocking: call it from a sync handler's thread, never on the
    loop nor in a work-pool thread, unless async code read the caller's list first
    (`offered`, from `entity_settings_access.offered_voices_async`)."""
    _check_style(avatar.style, avatar_catalog())
    if voice is None:
        raise IdentityRefused(400, f"{name}'s identity needs a voice: an identity someone chooses always names one.")
    _check_voice_offered(voice, name, request=request, for_conversation=for_conversation, offered=offered)
    _check_voice_downloaded(voice, name)


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


def record_payload(record: DigitalIdentity, catalog: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """A record as the doors serve it. `avatar.style` is the design drawn (a retired id resolved to
    its replacement), `avatar.style_requested` the id the record holds (equal when nothing was
    renamed), `avatar.watch_code` the drawn design's."""
    avatar = record.avatar.to_dict()
    style = resolve_style(record.avatar.style, catalog)
    avatar["style"] = style
    avatar["style_requested"] = record.avatar.style
    avatar["watch_code"] = str(catalog[style].get("watch_code") or "") or None
    return {
        "rev": record.rev,
        "avatar": avatar,
        "voice": record.voice.to_dict() if record.voice is not None else None,
        "set_at": record.set_at,
        "set_by": record.set_by,
    }


def identity_change(home_dir: Path, rev: int) -> Optional[Dict[str, Any]]:
    """Why and by whom revision `rev` was written: its `identity_changed` row in the home's biography
    (`state_history.jsonl`, the ADR 0052 §7 fields), as `{cause, written_by, at}`; None when the home
    has no row for it. One unreadable line never hides the others."""
    import json

    path = Path(home_dir) / "state_history.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    found: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("marker") == "identity_changed" and row.get("rev") == rev:
            found = {"cause": row.get("cause"), "written_by": row.get("written_by"), "at": row.get("at")}
    return found


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
    payload = record_payload(record, catalog)
    # The real reason of this revision (the Entity app shows it instead of inferring one).
    payload["change"] = identity_change(home_dir, record.rev)
    return {
        "entity_id": manifest.entity_id,
        "identity": payload,
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
        return record_payload(record, avatar_catalog())
    except (IdentityUnreadable, IdentityRefused) as e:
        logger.warning("identity: %s: not on the list row: %s", manifest.slug, e)
        return None


# --------------------------------------------------------------------------- the legacy files' view


def _shape_design(shape: str, catalog: Mapping[str, Mapping[str, Any]]) -> str:
    """The design a legacy shape writes: what the catalog draws for its retired `watch-shape-<shape>`
    (abstractavatar 0.2.0: kitty and face -> watch-halo, the default; wave -> watch-wave)."""
    return resolve_style(f"{_SHAPE_PREFIX}{shape}", catalog)


def derived_shape(style: str) -> str:
    """The legacy appearance shape of a design: the shape whose design it draws, when exactly one
    shape writes that design and it is not the catalog's default orb; every other design is the orb
    (`planet`). Without the avatar catalog no design resolves, and every look reads as the orb."""
    try:
        catalog = avatar_catalog()
    except IdentityRefused as e:  # logged where the catalog is needed; a listing reads it per row
        logger.debug("identity: the legacy shape of %s reads as the orb: %s", style, e.detail)
        return _PLANET
    drawn = resolve_style(style, catalog)
    shapes = [shape for shape in _SHAPES if _shape_design(shape, catalog) == drawn]
    return shapes[0] if len(shapes) == 1 and not catalog[drawn].get("default") else _PLANET


def style_for_shape(shape: str, current_style: str, entity_id: str) -> str:
    """The design a legacy `PUT /appearance` shape writes: `kitty|face|wave` -> the ready design the
    catalog draws for its retired `watch-shape-<shape>`; `planet` keeps an orb design, and replaces a
    shape design by the entity's recorded-default design when it is ready, else by the catalog's
    default (a deterministic ready orb, never a guess)."""
    catalog = avatar_catalog()
    if shape != _PLANET:
        return _shape_design(shape, catalog)
    if derived_shape(current_style) == _PLANET:
        return current_style
    avatar, _voice = default_identity(entity_id, None)
    if avatar.style in ready_designs(catalog):
        return avatar.style
    return resolve_style("", catalog)
