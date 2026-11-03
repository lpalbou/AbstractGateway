"""The order of `GET /api/gateway/automations` and the section each row belongs to (ADR 0048).

ONE rule, served to every client so none re-sorts by a rule of its own:

- `section` says where a row is drawn: `"active"` (every automation that is neither paused nor
  archived: status `active`, `completed` or `failed`), `"paused"` or `"archived"`.
- `last_activity_at` is the automation's last change as its controller run recorded it
  (`updated_at`): it moves on every controller tick (an occurrence admitted or finished, a retry,
  a pause, resume, cancel or revision, an archive), never on a read or a "seen" mark.
- A listing is ordered by section (active, then paused, then archived), then by
  `last_activity_at`, newest first, then by `automation_id` (descending) so ties are stable.

The keyset cursor names the last row of a page by that same key, so every page continues where
the previous one stopped. The key is mutable (an automation that runs moves up), so a row that
changes between two page reads may be served twice or not at all in that walk; the next refresh
shows it. Clients refresh from the first page and never resume an old walk.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar

SECTIONS: Tuple[str, ...] = ("active", "paused", "archived")
_RANK = {name: i for i, name in enumerate(SECTIONS)}
_CURSOR_PREFIX = "auto2:"

OrderKey = Tuple[int, str, str]
T = TypeVar("T")


class InvalidOrderCursor(ValueError):
    """A cursor this gateway did not issue (or one issued under the earlier creation order)."""


def automation_section(status: str) -> str:
    """`archived` and `paused` keep their own sections; every other status reads `active`."""
    if status == "archived":
        return "archived"
    if status == "paused":
        return "paused"
    return "active"


def order_key(*, status: str, last_activity_at: Any, automation_id: str) -> OrderKey:
    return (_RANK[automation_section(status)], str(last_activity_at or ""), str(automation_id))


def _sort_position(key: OrderKey) -> Tuple[int, Any, Any]:
    # Section ascending; activity and id descending (strings: compare their negated code points).
    return (key[0], tuple(-ord(c) for c in key[1]) + (1,), tuple(-ord(c) for c in key[2]) + (1,))


def ordered(entries: Iterable[Tuple[OrderKey, T]]) -> List[Tuple[OrderKey, T]]:
    """Entries sorted by the listing order."""
    return sorted(entries, key=lambda e: _sort_position(e[0]))


def encode_cursor(key: OrderKey) -> str:
    raw = json.dumps([key[0], key[1], key[2]], separators=(",", ":"))
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> OrderKey:
    try:
        if not cursor.startswith(_CURSOR_PREFIX):
            raise ValueError("prefix")
        rank, at, aid = json.loads(base64.urlsafe_b64decode(cursor[len(_CURSOR_PREFIX):].encode("ascii")))
        if not isinstance(rank, int) or rank not in _RANK.values():
            raise ValueError("rank")
        return (rank, str(at), str(aid))
    except Exception as exc:  # noqa: BLE001 - every malformed cursor is the same 422
        raise InvalidOrderCursor(f"invalid automations cursor {cursor!r}") from exc


def page(entries: Sequence[Tuple[OrderKey, T]], *, cursor: Optional[str], limit: int) -> Tuple[List[Tuple[OrderKey, T]], Optional[str]]:
    """One page of `ordered(...)` entries after `cursor`, and the cursor of the next page (None on
    the last one)."""
    rest: Sequence[Tuple[OrderKey, T]] = entries
    if cursor:
        after = _sort_position(decode_cursor(cursor))
        rest = [e for e in entries if _sort_position(e[0]) > after]
    lim = max(1, int(limit))
    out = list(rest[:lim])
    more = len(rest) > lim
    return out, (encode_cursor(out[-1][0]) if (more and out) else None)


def section_counts(statuses: Iterable[str]) -> Dict[str, int]:
    counts = {name: 0 for name in SECTIONS}
    for status in statuses:
        counts[automation_section(status)] += 1
    return counts


__all__ = [
    "SECTIONS",
    "InvalidOrderCursor",
    "automation_section",
    "decode_cursor",
    "encode_cursor",
    "order_key",
    "ordered",
    "page",
    "section_counts",
]
