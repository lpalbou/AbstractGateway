"""Read-aloud recordings have their own voice/ namespace, outside user files.

Classify earlier streamed artifacts from their producer metadata, never by
.wav extension or filename: uploaded recordings and requested audio exports
remain ordinary user files. This projection preserves immutable artifact IDs.
"""
from typing import Any


def is_voice_playback(meta: Any) -> bool:
    tags = getattr(meta, "tags", None) or {}
    if tags.get("purpose") == "voice_playback":
        return True
    if tags.get("kind") != "generated_media" or tags.get("modality") != "voice" or tags.get("task") != "tts":
        return False
    metadata = getattr(meta, "metadata", None) or {}
    capability = metadata.get("capability_metadata") or {}
    # Both local and remote stream finalizers record this producer block.
    stream = capability.get("stream") if isinstance(capability, dict) else None
    return isinstance(stream, dict) and stream.get("chunk_format") == "wav-segment"


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
