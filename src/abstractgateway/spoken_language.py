"""The spoken language of a transcription — ONE resolver (round 18).

``spoken_language`` is ``"auto"`` or an ISO 639-1 code the speech engines support (THE list is
AbstractVoice's: ``abstractvoice.stt.languages``). It is a per-ACCOUNT preference
(account_preferences.py, key ``spoken_language``; nothing stored = auto) and a per-request hint
(``language`` on ``POST /runs/{run_id}/audio/transcribe`` and on the OpenAI-compatible
``POST /v1/audio/transcriptions``). Every transcription entry point resolves what reaches the
engine HERE, in this order:

    the request's hint  →  the account's preference  →  auto (= ``None`` to the engine)

so a client never keeps a copy and never guesses from an OS locale. An unknown code is refused
with the voice layer's sentence, as a request (400) or as a preference write (preference_refused).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

AUTO = "auto"
KEY = "spoken_language"
LABEL = "Spoken language"
HELP = (
    "The language spoken to the microphone. Auto lets the speech engine detect it; naming it skips "
    "detection, so short phrases and mixed-language speech transcribe reliably and a little faster."
)


class SpokenLanguageError(ValueError):
    """An unknown code (the message is the voice layer's one sentence)."""


def _voice():
    """AbstractVoice's language list — THE source. A gateway whose AbstractVoice lacks it is a
    broken install (the floor is abstractvoice >= 0.15.0): fail loudly, never fall back to a list
    of our own."""
    from abstractvoice.stt import languages

    return languages


def normalize(value: Any) -> Optional[str]:
    """``None``/""/"auto" -> None (auto); a supported code -> the lower-case code; else
    SpokenLanguageError(sentence)."""
    try:
        return _voice().normalize_language(value)
    except ValueError as exc:
        raise SpokenLanguageError(str(exc)) from exc


def as_value(code: Optional[str]) -> str:
    """The wire value of a code: ``"auto"`` for None."""
    return code or AUTO


def choices() -> List[Dict[str, str]]:
    """``[{"value": "auto", "label": "Auto (detected)"}, {"value": "en", "label": "English"}, ...]``."""
    return _voice().choices()


def supported_codes() -> List[str]:
    return _voice().supported_languages()


def label(code: Optional[str]) -> str:
    return _voice().language_label(code)


def preferences_block(stored: Optional[str]) -> Dict[str, Any]:
    """The preferences GET's ``spoken_language`` block (the control's whole truth, served):
    ``{value, label, help, choices}`` — ``value`` is "auto" or the stored code, never null."""
    return {"value": as_value(stored), "label": LABEL, "help": HELP, "choices": choices()}


@dataclass(frozen=True)
class Resolved:
    """What reaches the engine for ONE transcription."""

    #: The code, or None = auto (the engine detects the language).
    language: Optional[str]
    #: "request" (the hint), "account" (the preference) or "auto" (neither).
    source: str

    @property
    def value(self) -> str:
        return as_value(self.language)

    def evidence(self, detected_language: Optional[str] = None) -> Dict[str, Any]:
        """The ledger/route facts: ``{language, language_source, detected_language}``."""
        return {"language": self.value, "language_source": self.source, "detected_language": detected_language}


def resolve(
    request_hint: Any,
    *,
    data_dir: Path,
    tenant_id: Optional[str],
    user_id: Optional[str],
) -> Resolved:
    """THE resolver: the request's ``language`` hint (validated), else the account's stored
    preference, else auto. An unknown hint raises SpokenLanguageError (the caller answers 400)."""
    hint = normalize(request_hint)
    if hint is not None:
        return Resolved(language=hint, source="request")
    from .account_preferences import stored_spoken_language

    stored = stored_spoken_language(Path(data_dir), tenant_id=str(tenant_id or "default"), user_id=str(user_id or ""))
    if stored:
        return Resolved(language=stored, source="account")
    return Resolved(language=None, source="auto")


def detected_language_of(result: Any) -> Optional[str]:
    """The engine's reported language in a completed transcription child result (the runtime
    puts it in ``metadata.detected_language``), or None."""
    meta = result.get("metadata") if isinstance(result, dict) else None
    value = meta.get("detected_language") if isinstance(meta, dict) else None
    return str(value).strip().lower() if isinstance(value, str) and value.strip() else None


__all__ = [
    "AUTO",
    "HELP",
    "KEY",
    "LABEL",
    "Resolved",
    "SpokenLanguageError",
    "as_value",
    "choices",
    "detected_language_of",
    "label",
    "normalize",
    "preferences_block",
    "resolve",
    "supported_codes",
]
