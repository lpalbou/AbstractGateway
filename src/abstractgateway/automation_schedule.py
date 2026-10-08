"""What an automation's schedule SAYS, and when it runs next, as the gateway serves it (R16.1).

Every client (Code web/TUI, Observer, the Assistant, both consoles, the kit AutomationPanel)
shows these served values verbatim and never computes a next run or rebuilds the sentence:

- ``time_zone``: the schedule's IANA zone — a ``schedule@2`` binding's ``config.time_zone``; for
  any other trigger (``schedule@1``, legacy roots, email, manual) the OWNER's account time zone.
- ``next_run_at``: the runtime's projection (``next_fire_at``), UTC ISO; absent when none.
- ``next_run_local``: the same instant as ISO with the zone's offset; absent when none.
- ``schedule_rule_text``: the rule in one sentence, e.g. "Every day at 08:00 (Europe/Paris)".
- ``schedule_text``: the rule + " · next Thu 9 Oct 08:00" when a next run exists.

The owner's time zone is the account preference ``time_zone`` (account_preferences.py); its
default is THIS host's IANA zone (``host_time_zone``), never guessed by a client.
"""

from __future__ import annotations

import copy
import datetime
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from abstractruntime.triggers.protocol import TriggerConfigError
from abstractruntime.triggers.schedule import CALENDAR_KINDS, WEEKDAYS, time_zone_names, validate_time_zone

UTC = "UTC"
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # fixed English, never the host locale
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAY_SHORT = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def _zone_or_none(name: Any) -> Optional[str]:
    try:
        return validate_time_zone(name)
    except TriggerConfigError:
        return None


def host_time_zone(*, environ: Optional[Mapping[str, str]] = None, localtime: str = "/etc/localtime", timezone_file: str = "/etc/timezone") -> str:
    """This host's IANA time zone: the OS's ``TZ`` (when it names an IANA zone), else the zone
    ``/etc/localtime`` links to (macOS, most Linux), else ``/etc/timezone`` (Debian), else "UTC"."""
    env = os.environ if environ is None else environ
    raw = str(env.get("TZ") or "").strip().lstrip(":")
    zone = _zone_or_none(raw) if raw else None
    if zone:
        return zone
    try:
        target = os.path.realpath(localtime)
    except OSError:
        target = ""
    if "/zoneinfo/" in target:
        zone = _zone_or_none(target.split("/zoneinfo/", 1)[1])
        if zone:
            return zone
    try:
        zone = _zone_or_none(Path(timezone_file).read_text(encoding="utf-8").strip())
    except OSError:
        zone = None
    return zone or UTC


def owner_time_zone(root_data_dir: Any, *, tenant_id: str, user_id: str) -> str:
    """The account's ``time_zone`` preference, else the gateway default (this host's zone)."""
    from .account_preferences import stored_time_zone

    try:
        stored = stored_time_zone(Path(root_data_dir), tenant_id=tenant_id, user_id=user_id) if user_id else None
    except Exception:  # noqa: BLE001 - an unreadable store falls back to the gateway default
        stored = None
    return stored or host_time_zone()


def is_schedule_v2(trigger: Any) -> bool:
    return isinstance(trigger, Mapping) and str(trigger.get("source_id") or "") == "schedule" and trigger.get("source_version") == 2


def with_time_zone(trigger: Any, zone: str) -> Any:
    """A ``schedule@2`` trigger request with ``config.time_zone`` filled in when the client sent
    none (any other trigger is returned unchanged)."""
    if not is_schedule_v2(trigger):
        return trigger
    out = copy.deepcopy(dict(trigger))
    config = out.get("config")
    if isinstance(config, Mapping) and config.get("time_zone") in (None, ""):
        out["config"] = {**dict(config), "time_zone": zone}
    elif config is None:
        out["config"] = {"time_zone": zone}
    return out


def trigger_time_zone(trigger: Any, owner_zone: str) -> str:
    if is_schedule_v2(trigger):
        zone = _zone_or_none((trigger.get("config") or {}).get("time_zone"))
        if zone:
            return zone
    return owner_zone


# ------------------------------------------------------------------- wording


def _interval_words(every: Any) -> str:
    """`8h` -> "every 8 hours" (fixed UTC intervals; same words as the occurrence summary)."""
    words = {"s": ("second", "seconds"), "m": ("minute", "minutes"), "h": ("hour", "hours"), "d": ("day", "days")}
    text = str(every or "")
    unit, amount = text[-1:], text[:-1]
    if unit not in words or not amount.isdigit():
        return f"every {text}"
    n = int(amount)
    return f"every {words[unit][0]}" if n == 1 else f"every {n} {words[unit][1]}"


def _parse_utc(iso: Any) -> Optional[datetime.datetime]:
    if not isinstance(iso, str) or not iso.strip():
        return None
    raw = iso.strip()
    try:
        dt = datetime.datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def local_short(iso: Any, zone: str, *, now: Optional[datetime.datetime] = None) -> str:
    """"Thu 9 Oct 08:00" in ``zone`` (the year is added when it is not the current one there)."""
    dt = _parse_utc(iso)
    if dt is None:
        return str(iso or "")
    tz = ZoneInfo(zone)
    local = dt.astimezone(tz)
    current = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
    year = f" {local.year}" if local.year != current.year else ""
    return f"{_WEEKDAY_ABBR[local.weekday()]} {local.day} {_MONTH_ABBR[local.month - 1]}{year} {local.hour:02d}:{local.minute:02d}"


def local_iso(iso: Any, zone: str) -> Optional[str]:
    dt = _parse_utc(iso)
    return dt.astimezone(ZoneInfo(zone)).isoformat() if dt is not None else None


def _days_text(days: Any) -> str:
    names = [_DAY_SHORT[d] for d in WEEKDAYS if d in (days or [])]
    if not names:
        return "?"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _bounds(config: Mapping[str, Any], zone: str, now: Optional[datetime.datetime]) -> str:
    parts = []
    count = config.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        parts.append(f"{count} {'run' if count == 1 else 'runs'} max")
    if config.get("until"):
        parts.append(f"until {local_short(config['until'], zone, now=now)}")
    return "".join(f" · {p}" for p in parts)


def rule_text(trigger: Any, zone: str, *, now: Optional[datetime.datetime] = None) -> str:
    """The trigger in one sentence (see the module docstring); ``zone`` = ``trigger_time_zone``."""
    if not isinstance(trigger, Mapping):
        return ""
    source = str(trigger.get("source_id") or "")
    config = trigger.get("config") if isinstance(trigger.get("config"), Mapping) else {}
    if source == "manual":
        return "Manual runs only"
    if source == "email.received":
        return "When an email arrives"
    if source != "schedule":
        return f"{source}@{trigger.get('source_version')}"
    kind = config.get("kind") if trigger.get("source_version") == 2 else None
    if kind is None:
        kind = "every" if config.get("every") else "once"
    if kind == "every":
        return f"{_interval_words(config.get('every')).capitalize()} (UTC){_bounds(config, zone, now)}"
    if kind == "once":
        if not config.get("start_at"):
            return "Once, now"
        return f"Once at {local_short(config['start_at'], zone, now=now)} ({zone})"
    at = str(config.get("at") or "")
    if kind == "daily":
        head = "Every day"
    elif kind == "weekly":
        head = f"Every {_days_text(config.get('days'))}"
    elif kind == "monthly":
        day = config.get("day")
        if day == "last":
            head = "Monthly on the last day"
        elif isinstance(day, int) and day > 28:
            head = f"Monthly on day {day} (or the last day)"
        else:
            head = f"Monthly on day {day}"
    else:
        return f"schedule ({kind})"
    return f"{head} at {at} ({zone}){_bounds(config, zone, now)}"


def schedule_fields(trigger: Any, next_fire_at: Optional[str], owner_zone: str, *, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """The summary's schedule block: ``time_zone``, ``schedule_rule_text``, ``schedule_text`` and,
    when a next run exists, ``next_run_at`` + ``next_run_local``."""
    zone = trigger_time_zone(trigger, owner_zone)
    rule = rule_text(trigger, zone, now=now)
    out: Dict[str, Any] = {"time_zone": zone, "schedule_rule_text": rule, "schedule_text": rule}
    if next_fire_at:
        out["next_run_at"] = str(next_fire_at)
        out["next_run_local"] = local_iso(next_fire_at, zone)
        out["schedule_text"] = f"{rule} · next {local_short(next_fire_at, zone, now=now)}"
    return out


def first_run_sentence(trigger: Any, zone: str, next_run_at: Optional[str], *, now: datetime.datetime) -> str:
    """The dialog's line before saving: "Runs every day at 08:00 (Europe/Paris), first run Thu 9 Oct
    08:00." / "Runs every 24 hours (UTC), first run now." / "Runs once at … ." / "Runs when an email arrives." """
    rule = rule_text(trigger, zone, now=now)
    head = "Runs " + rule[:1].lower() + rule[1:] if rule else "Runs"
    source = str((trigger or {}).get("source_id") or "") if isinstance(trigger, Mapping) else ""
    config = (trigger or {}).get("config") if isinstance(trigger, Mapping) else {}
    kind = (config or {}).get("kind") or ("every" if (config or {}).get("every") else "once")
    if source != "schedule" or kind == "once":
        return f"{head}."
    nxt = _parse_utc(next_run_at)
    if nxt is None:
        return f"{head}, no run left."
    when = "now" if nxt <= now + datetime.timedelta(seconds=1) else local_short(next_run_at, zone, now=now)
    return f"{head}, first run {when}."


def preferences_time_zone_block(stored: Optional[str]) -> Dict[str, Any]:
    """The preferences GET's ``time_zone`` block (the picker's whole truth, served)."""
    default = host_time_zone()
    return {
        "value": stored,
        "gateway_default": default,
        "effective": stored or default,
        "label": "Time zone",
        "help": "Daily, weekly and monthly automations run on this clock. Gateway default follows this computer's time zone.",
        "choices": time_zone_names(),
    }


__all__ = [
    "CALENDAR_KINDS",
    "first_run_sentence",
    "host_time_zone",
    "is_schedule_v2",
    "local_iso",
    "local_short",
    "owner_time_zone",
    "preferences_time_zone_block",
    "rule_text",
    "schedule_fields",
    "trigger_time_zone",
    "with_time_zone",
]
