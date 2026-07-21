"""Per-entity voice — laurent's directive (dm:gateway--laurent#10, 2026-07-17).

Each entity can have its own voice. The choice lives IN THE HOME
(`<home>/voice.yaml`) because the home IS the entity's runtime plane (the
per-home run store lives inside it; directory copy moves the whole life) —
laurent's "voice is a property of the runtime, not the gateway" satisfied
for entities exactly the way per-principal capability defaults satisfy it
for users. Adversarial review 2026-07-17 (two seats), key rulings folded:

- FULL TRIPLE ALWAYS: {provider, model, voice} — a bare voice id is only
  meaningful to its backend, and merging a naked voice into a spec that
  later receives a different provider recreates the M1 cross-provider leak
  (the 'Unknown voice_id: M1' incident). voice.yaml refuses partial writes.
- OPPOSITE FAILURE SEMANTICS from substrate.yaml, deliberately a SEPARATE
  file: a missing/unresolvable substrate REFUSES the summon (a truncated
  mind is a different person); a missing voice DEGRADES gracefully down
  the chain (request > entity > user/gateway capability default > engine
  default) — laurent's explicit ask. One file cannot carry both contracts.
- ANTI-MIXING RESOLUTION: the home triple applies ONLY when the request
  names NONE of provider/model/voice/profile. A request naming any subset
  passes through untouched — filling the gaps from the home would mix
  provider/voice identities across sources (the leak class again).
- The generic /runs/{id}/voice/tts* routes stay ENTITY-BLIND: resolution
  happens in the entity-owned endpoints (routes/entities.py), which mint
  their run scope server-side — never trusting a client-crafted scope
  string to claim an entity's voice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

VOICE_FILENAME = "voice.yaml"


def entity_voice_path(home_dir: Any) -> Path:
    return Path(home_dir) / VOICE_FILENAME


def read_entity_voice(home_dir: Any) -> Dict[str, Any]:
    """The stored voice choice: full triple or {}. Malformed/partial files
    read as UNSET (the chain continues to user/gateway defaults — degrade,
    never refuse; a voice is presentation, not the mind)."""
    path = entity_voice_path(home_dir)
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    provider = str(data.get("provider") or "").strip()
    model = str(data.get("model") or "").strip()
    voice = str(data.get("voice") or "").strip()
    if not (provider and model and voice):
        return {}
    out: Dict[str, Any] = {"provider": provider, "model": model, "voice": voice}
    speed = data.get("speed")
    if isinstance(speed, (int, float)) and not isinstance(speed, bool) and float(speed) > 0:
        out["speed"] = float(speed)
    preset = str(data.get("quality_preset") or "").strip()
    if preset:
        out["quality_preset"] = preset
    return out


def write_entity_voice(
    home_dir: Any,
    *,
    provider: str,
    model: str,
    voice: str,
    speed: Optional[float] = None,
    quality_preset: Optional[str] = None,
) -> None:
    """Persist the voice choice — full triple required, atomic write."""
    import os
    import tempfile

    import yaml

    p = str(provider or "").strip()
    m = str(model or "").strip()
    v = str(voice or "").strip()
    if not (p and m and v):
        raise ValueError(
            "an entity voice needs provider AND model AND voice — a voice id is only "
            "meaningful to its backend (the cross-provider leak class); to unselect, "
            "delete the choice instead"
        )
    doc: Dict[str, Any] = {"provider": p, "model": m, "voice": v}
    if speed is not None:
        if not (isinstance(speed, (int, float)) and not isinstance(speed, bool) and float(speed) > 0):
            raise ValueError("speed must be a positive number")
        doc["speed"] = float(speed)
    if quality_preset is not None and str(quality_preset).strip():
        doc["quality_preset"] = str(quality_preset).strip()
    home = Path(home_dir)
    fd, tmp_name = tempfile.mkstemp(prefix=".voice_", suffix=".tmp", dir=str(home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(yaml.safe_dump(doc, sort_keys=True, allow_unicode=True))
        os.replace(tmp_name, str(entity_voice_path(home)))
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


def clear_entity_voice(home_dir: Any) -> bool:
    """Remove the choice (the entity falls back down the chain). Returns
    whether a file existed. Deletion of a PRESENTATION preference is not a
    life mutation — the never-purge rule guards the life stores, not
    operator config files (tool_policy/substrate have the same property)."""
    path = entity_voice_path(home_dir)
    if not path.is_file():
        return False
    path.unlink()
    return True


def resolve_entity_voice_fields(
    home_dir: Any,
    *,
    request_provider: Optional[str] = None,
    request_model: Optional[str] = None,
    request_voice: Optional[str] = None,
    request_profile: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """The anti-mixing resolution: (fields_to_apply, voice_source).

    - request names ANY of provider/model/voice/profile -> ({}, "request"):
      the request passes through untouched; mixing home fields into a
      partially-specified request is the cross-provider leak class.
    - request names none + home has a triple -> (triple, "entity").
    - neither -> ({}, "unset"): downstream capability defaults / engine
      default resolve it (the gateway keeps a default — laurent's ask).
    """
    if any(
        str(x or "").strip()
        for x in (request_provider, request_model, request_voice, request_profile)
    ):
        return {}, "request"
    stored = read_entity_voice(home_dir)
    if stored:
        return dict(stored), "entity"
    return {}, "unset"
