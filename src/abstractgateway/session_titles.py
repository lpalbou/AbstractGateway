"""Session (conversation) titles: the name a person gives a conversation.

Without one, every client names a conversation after its opening message. A stored
title replaces that name everywhere `GET /runs` is read; clearing it brings the
automatic name back. Nothing about the session changes: its runs, ledgers and the
opening message stay as they are. The titles live in the principal's runtime data
dir (`<data_dir>/session_titles.json`, the same plane as the session's runs, like
`session_archive.json`):

    {"schema": "gateway_session_titles_v1",
     "sessions": {session_id: {"title", "set_at", "set_by": {"kind": "person", "user_id"}}}}

`set_by.kind` says who named the conversation ("person" for the rename route), so a
later writer (a model-proposed title, gateway backlog 0937) can tell a person's name
from its own. Reads, locking and the atomic write are `json_store.py`'s.

Failure behaviour:
- listings (`GET /runs`, search) never fail because of this file: they are served
  without titles and say why (`titles_for_listing` -> `session_titles_error`);
- a file that is not JSON at all (damaged) is set aside as
  `session_titles.damaged-<UTC>.json` by the next rename, which then starts a fresh store;
- a valid JSON file of another shape (a newer schema, a foreign file) is never touched:
  a rename answers 500 `session_titles_unreadable`.
"""

from __future__ import annotations

import datetime
import logging
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from .json_store import JsonStore, StoreBusy, StoreUnreadable, StoreUnwritable

SCHEMA = "gateway_session_titles_v1"
FILENAME = "session_titles.json"
MAX_CHARS = 120
# Hostile input is bounded before any work: a longer `title` is cut to its first MAX_INPUT_CHARS.
MAX_INPUT_CHARS = 4096
REVALIDATE_S = 1.0

# Format characters (Unicode category Cf) hide text or make a title imitate another one in a list
# shown as plain text (bidirectional controls, zero-width space, word joiner, BOM, soft hyphen, tag
# characters, invisible operators, interlinear annotation): all are removed, except the ones scripts
# and emoji need: the joiners U+200C/U+200D, the direction marks U+200E, U+200F, U+061C, the
# visible number signs of Arabic, Syriac and Kaithi (_VISIBLE_FORMAT), and the tag characters of an
# emoji tag sequence (U+1F3F4 followed by tags ending with U+E007F: the flags of England, Scotland, Wales).
_VISIBLE_FORMAT = frozenset({*range(0x0600, 0x0606), 0x06DD, 0x070F, 0x0890, 0x0891, 0x08E2, 0x110BD, 0x110CD})
_KEPT_FORMAT = frozenset({0x200C, 0x200D, 0x200E, 0x200F, 0x061C}) | _VISIBLE_FORMAT
_BLACK_FLAG, _TAG_FIRST, _TAG_LAST, _CANCEL_TAG = "\U0001F3F4", 0xE0020, 0xE007F, "\U000E007F"
# Letters that render as blank (Hangul fillers, Braille blank).
_BLANK_LETTERS = frozenset({0x3164, 0x115F, 0x1160, 0xFFA0, 0x2800})

_log = logging.getLogger("abstractgateway.session_titles")


class SessionTitlesUnreadable(RuntimeError):
    """The titles file exists but cannot be read as the schema."""


class SessionTitlesUnwritable(RuntimeError):
    """The titles file could not be saved; it was left as it was."""


class SessionTitlesBusy(SessionTitlesUnwritable):
    """Another writer held the titles file past json_store.LOCK_WAIT_S; try again."""


def _visible(c: str) -> bool:
    """A character that shows something: not a mark, separator, control or format character, a kept
    joiner or direction mark, a variation selector, or a letter that renders blank."""
    o = ord(c)
    if o in _VISIBLE_FORMAT:
        return True
    if o in _BLANK_LETTERS or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
        return False
    return unicodedata.category(c)[0] not in "MZC"


def normalize_title(raw: Optional[str]) -> Optional[str]:
    """One line, at most MAX_CHARS characters. The input is first cut to MAX_INPUT_CHARS; format
    characters (Cf) other than `_KEPT_FORMAT` and emoji tag sequences are removed; control characters (line breaks, tabs,
    C0, DEL, C1) and unpaired surrogates become spaces; runs of whitespace become one space; the
    result is trimmed and cut on a character boundary. None when no visible character is left
    (`_visible`): the title is cleared."""
    if raw is None:
        return None
    if len(raw) <= MAX_CHARS and raw.isprintable() and raw == " ".join(raw.split()):
        # Already one clean line (every stored title is: printable excludes every control, every
        # format character and every space but U+0020). Keeps a load of many titles cheap.
        text = raw
    else:
        out: List[str] = []
        s = raw[:MAX_INPUT_CHARS]
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            o = ord(c)
            if _TAG_FIRST <= o <= _TAG_LAST:
                # A run of tag characters: kept whole only as an emoji tag sequence (after U+1F3F4,
                # ending with the cancel tag), removed anywhere else.
                j = i
                while j < n and _TAG_FIRST <= ord(s[j]) <= _TAG_LAST:
                    j += 1
                run = s[i:j]
                if out and out[-1] == _BLACK_FLAG and run.endswith(_CANCEL_TAG) and len(run) > 1:
                    out.append(run)
                i = j
                continue
            if o < 0x20 or 0x7F <= o < 0xA0 or 0xD800 <= o < 0xE000:
                out.append(" ")
            elif o not in _KEPT_FORMAT and unicodedata.category(c) == "Cf":
                pass
            else:
                out.append(c)
            i += 1
        text = " ".join("".join(out).split())
        text = text[:MAX_CHARS].rstrip()
    return text if any(_visible(c) for c in text) else None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _valid(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA and isinstance(doc.get("sessions"), dict)


def _load(data_dir: Any) -> Dict[str, Any]:
    return _STORE.load(data_dir)


_STORE = JsonStore(FILENAME, empty=lambda: {"schema": SCHEMA, "sessions": {}}, valid=_valid,
                   reader=lambda d: _load(d), revalidate_s=lambda: REVALIDATE_S)


def _titles_of(doc: Dict[str, Any]) -> Dict[str, str]:
    """`{session_id: title}`, normalised on load too (a hand-edited file is served like a stored
    title); records of another shape are skipped."""
    out: Dict[str, str] = {}
    for sid, rec in doc["sessions"].items():
        title = normalize_title(rec.get("title")) if isinstance(rec, dict) and isinstance(rec.get("title"), str) else None
        if title:
            out[str(sid)] = title
    return out


def session_titles(data_dir: Any) -> Dict[str, str]:
    """`{session_id: title}` for this plane (computed once per revision of the file; never mutate it).
    Raises SessionTitlesUnreadable."""
    try:
        return _STORE.derived(data_dir, "titles", _titles_of)
    except StoreUnreadable as exc:
        raise SessionTitlesUnreadable(str(exc)) from None


def folded_titles(data_dir: Any) -> List[Tuple[str, str]]:
    """`[(session_id, title as the search index folds text)]`, once per revision of the file."""
    from abstractruntime.conversation_search import normalize

    try:
        return _STORE.derived(data_dir, "folded", lambda doc: [(sid, normalize(t)) for sid, t in _titles_of(doc).items()])
    except StoreUnreadable as exc:
        raise SessionTitlesUnreadable(str(exc)) from None


_LAST_STATE: Dict[str, Optional[str]] = {}


def titles_for_listing(data_dir: Any) -> Tuple[Dict[str, str], Optional[Dict[str, str]]]:
    """For listings: `(titles, None)`, or `({}, session_titles_error)` when the file cannot be read.
    The state is logged once per change (broken -> fine and back), never per request."""
    key = str(_STORE.path(data_dir))
    try:
        titles, error = session_titles(data_dir), None
    except SessionTitlesUnreadable as exc:
        titles = {}
        error = {"reason_code": "session_titles_unreadable",
                 "message": f"Conversation names could not be read ({exc}); conversations are listed without them."}
    message = error["message"] if error else None
    if _LAST_STATE.get(key, None) != message:
        if message:
            _log.warning("session titles: %s", message)
        elif key in _LAST_STATE:
            _log.info("session titles: %s is readable again", FILENAME)
        _LAST_STATE[key] = message
    return titles, error


def set_session_title(data_dir: Any, session_id: str, title: Optional[str], *, by: str) -> Dict[str, Any]:
    """Store a person's title for `session_id` (normalised; None or blank clears it). Storing the
    title already stored, or clearing none, changes nothing (`changed: false`, the file untouched).
    Returns `{session_id, title, changed}`, plus `store_reset` (the name a damaged store was set
    aside as) when this write started a fresh store. Raises SessionTitlesUnreadable,
    SessionTitlesBusy or SessionTitlesUnwritable."""
    sid = str(session_id)
    value = normalize_title(title)

    def change(data: Dict[str, Any]) -> Any:
        current = data["sessions"].get(sid)
        stored = normalize_title(current.get("title")) if isinstance(current, dict) and isinstance(current.get("title"), str) else None
        if stored == value:
            return {"session_id": sid, "title": value, "changed": False}, False
        if value is None:
            data["sessions"].pop(sid, None)
        else:
            data["sessions"][sid] = {"title": value, "set_at": _now(), "set_by": {"kind": "person", "user_id": str(by) or None}}
        return {"session_id": sid, "title": value, "changed": True}, True

    try:
        out, aside = _STORE.update(data_dir, change, heal_damaged=True)
    except StoreUnreadable as exc:
        raise SessionTitlesUnreadable(str(exc)) from None
    except StoreBusy as exc:
        raise SessionTitlesBusy(str(exc)) from None
    except StoreUnwritable as exc:
        raise SessionTitlesUnwritable(str(exc)) from None
    if aside is not None:
        out["changed"] = True  # a fresh store was written
        out["store_reset"] = aside
    return out


def drop_session_title(data_dir: Any, session_id: str) -> bool:
    """Remove a session's title when the session itself is gone (its last run purged), so a later
    conversation reusing the id does not inherit it. True when one was removed."""
    sid = str(session_id)

    def change(data: Dict[str, Any]) -> Any:
        removed = data["sessions"].pop(sid, None) is not None
        return removed, removed

    try:
        return bool(_STORE.update(data_dir, change)[0])
    except StoreUnreadable as exc:
        raise SessionTitlesUnreadable(str(exc)) from None
    except StoreBusy as exc:
        raise SessionTitlesBusy(str(exc)) from None
    except StoreUnwritable as exc:
        raise SessionTitlesUnwritable(str(exc)) from None


__all__ = [
    "FILENAME",
    "MAX_CHARS",
    "MAX_INPUT_CHARS",
    "SCHEMA",
    "SessionTitlesBusy",
    "SessionTitlesUnreadable",
    "SessionTitlesUnwritable",
    "drop_session_title",
    "folded_titles",
    "normalize_title",
    "session_titles",
    "set_session_title",
    "titles_for_listing",
]
