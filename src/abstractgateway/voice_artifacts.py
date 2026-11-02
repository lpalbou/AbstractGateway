"""Read-aloud recordings have their own voice/ namespace, outside user files.

Classify earlier streamed artifacts from their producer metadata, never by
.wav extension or filename: uploaded recordings and requested audio exports
remain ordinary user files. This projection preserves immutable artifact IDs.

Voice transport is never an attachment (ADR 0047, 2026-11-02): the recording
of a spoken question (`purpose: voice_dictation`, uploaded under the reserved
`voice/dictation/` client path) and the read-aloud audio of an answer
(`purpose: voice_playback`) are how the conversation travelled, not part of
it. `is_voice_transport` is the one rule every default conversation listing
filters with; the runtime's history bundle applies the same rule
(abstractruntime.history_bundle.is_voice_transport) to session turns.
"""
from typing import Any, Dict

VOICE_DICTATION_PREFIX = "voice/dictation/"
VOICE_TRANSPORT_PURPOSES = frozenset({"voice_playback", "voice_dictation"})
VOICE_TRANSPORT_PATH_PREFIXES = ("voice/", "client:voice/")


def _tags_of(meta_or_tags: Any) -> Dict[str, Any]:
    if isinstance(meta_or_tags, dict):
        return meta_or_tags
    return getattr(meta_or_tags, "tags", None) or {}


def _path_tag(tags: Dict[str, Any], key: str) -> str:
    return str(tags.get(key) or "").strip().replace("\\", "/")


def is_voice_playback(meta: Any) -> bool:
    tags = _tags_of(meta)
    if tags.get("purpose") == "voice_playback":
        return True
    if tags.get("kind") != "generated_media" or tags.get("modality") != "voice" or tags.get("task") != "tts":
        return False
    metadata = getattr(meta, "metadata", None) or {}
    capability = metadata.get("capability_metadata") or {}
    # Both local and remote stream finalizers record this producer block.
    stream = capability.get("stream") if isinstance(capability, dict) else None
    return isinstance(stream, dict) and stream.get("chunk_format") == "wav-segment"


def is_voice_dictation(meta_or_tags: Any) -> bool:
    """A spoken question's recording: tagged `purpose: voice_dictation`, or uploaded under `voice/dictation/`."""
    tags = _tags_of(meta_or_tags)
    if tags.get("purpose") == "voice_dictation":
        return True
    for key in ("path", "source_path"):
        value = _path_tag(tags, key)
        if value.startswith("client:"):
            value = value[len("client:"):]
        if value.startswith(VOICE_DICTATION_PREFIX):
            return True
    return False


def is_voice_transport(meta_or_tags: Any) -> bool:
    """True for voice playback or dictation: `purpose` in VOICE_TRANSPORT_PURPOSES, `artifact_space: voice`,
    or a path under `voice/` / `client:voice/` (same rule as abstractruntime.history_bundle.is_voice_transport,
    plus the legacy stream-provenance classification of `is_voice_playback`)."""
    tags = _tags_of(meta_or_tags)
    if str(tags.get("purpose") or "").strip() in VOICE_TRANSPORT_PURPOSES:
        return True
    if str(tags.get("artifact_space") or "").strip() == "voice":
        return True
    for key in ("path", "source_path"):
        if _path_tag(tags, key).startswith(VOICE_TRANSPORT_PATH_PREFIXES):
            return True
    return is_voice_playback(meta_or_tags) or is_voice_dictation(tags)


def dictation_tags(source_path: Any) -> Dict[str, str]:
    """The tags an upload under the reserved `voice/dictation/` path is stamped with (else nothing)."""
    if is_voice_dictation({"source_path": str(source_path or "")}):
        return {"purpose": "voice_dictation", "artifact_space": "voice"}
    return {}


def voice_tags(meta: Any) -> dict:
    tags = dict(getattr(meta, "tags", None) or {})
    if is_voice_playback(meta):
        filename = tags.get("filename") or f"tts-{meta.artifact_id}.wav"
        tags.update(purpose="voice_playback", artifact_space="voice")
        # Historical streams did not carry a logical filename/path. Namespace
        # their listing without renaming blobs or breaking saved references.
        tags.setdefault("filename", filename)
        if not str(tags.get("path") or "").startswith("voice/"):
            tags["path"] = f"voice/tts-{meta.artifact_id}.wav"
    return tags
