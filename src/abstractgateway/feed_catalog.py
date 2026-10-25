"""The catalogue of well-known public feeds the Triggers page proposes (New connection → Feed).

Two layers, merged by `id`:

- SHIPPED: `abstractgateway/assets/feeds.json` (in the wheel), open RSS/Atom feeds verified live
  with the runtime's own fetcher and parser (`scripts/check_feed_catalog.py` re-verifies them);
- CUSTOM: `<data_dir>/catalog/feeds.json` when the file exists, edited by the person who runs the
  gateway. An entry whose `id` is shipped replaces it, a new `id` adds a feed, and
  `{"id": "<shipped id>", "disabled": true}` hides a shipped one. A custom entry needs `id`,
  `name` and `url` (http or https); `category` defaults to "My feeds". A malformed file or entry
  is skipped and named in `problems`; the shipped catalogue is still served.

Shape (`{"version": 1, "feeds": [entry]}`), one entry:

    {"id", "name", "url", "site", "category", "language", "description"}
    + for a template (an address the person completes):
    "template": true, "placeholder": {"key", "label", "example", "pattern"?}, "name_template"?

A template's `url` holds `{<key>}` exactly once; the console asks for the value in one field
(checked against `pattern` when given) and builds the address. The served list
(`GET /api/gateway/triggers/feeds/catalog`) carries `source: "shipped" | "custom"` per entry.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

CATALOG_VERSION = 1
CUSTOM_RELATIVE_PATH = Path("catalog") / "feeds.json"
DEFAULT_CUSTOM_CATEGORY = "My feeds"
SHIPPED_REQUIRED = ("id", "name", "url", "site", "category", "language", "description")
CUSTOM_REQUIRED = ("id", "name", "url")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def shipped_catalog() -> Dict[str, Any]:
    """The catalogue in the wheel (raises when the asset is missing or not JSON: a packaging bug)."""
    raw = (resources.files("abstractgateway") / "assets" / "feeds.json").read_text(encoding="utf-8")
    return json.loads(raw)


def custom_catalog_path(data_dir: Path) -> Path:
    return Path(data_dir) / CUSTOM_RELATIVE_PATH


def entry_problems(entry: Any, *, shipped: bool) -> List[str]:
    """Why `entry` cannot be listed ([] when it can). Shipped entries are held to https and every
    field; custom ones to `id`, `name` and an http(s) `url`."""
    if not isinstance(entry, dict):
        return ["an entry is not an object"]
    eid = entry.get("id")
    if not isinstance(eid, str) or not _ID_RE.match(eid):
        return [f"id {eid!r} must be lowercase letters, digits, '.', '_' or '-' (at most 64)"]
    out: List[str] = []
    for key in SHIPPED_REQUIRED if shipped else CUSTOM_REQUIRED:
        val = entry.get(key)
        if not isinstance(val, str) or not val.strip():
            out.append(f"{eid}: {key} is required (a non-empty string)")
    for key in ("site", "category", "language", "description", "name_template"):
        if key in entry and not isinstance(entry[key], str):
            out.append(f"{eid}: {key} must be a string")
    url = entry.get("url")
    if isinstance(url, str) and url.strip():
        parsed = urlparse(url)
        schemes = ("https",) if shipped else ("http", "https")
        if parsed.scheme not in schemes or not parsed.netloc or any(c.isspace() for c in url):
            out.append(f"{eid}: url must be an {' or '.join(schemes)} address")
        keys = _PLACEHOLDER_RE.findall(url)
        if entry.get("template") is True:
            ph = entry.get("placeholder")
            if not isinstance(ph, dict) or not all(isinstance(ph.get(k), str) and ph.get(k).strip() for k in ("key", "label", "example")):
                out.append(f"{eid}: a template needs placeholder {{key, label, example}}")
            else:
                if keys != [ph["key"]]:
                    out.append(f"{eid}: url must hold {{{ph['key']}}} exactly once")
                pattern = ph.get("pattern")
                if pattern is not None:
                    try:
                        if not isinstance(pattern, str) or not re.fullmatch(pattern, ph["example"]):
                            out.append(f"{eid}: placeholder example does not match its pattern")
                    except re.error:
                        out.append(f"{eid}: placeholder pattern is not a regular expression")
        elif "template" in entry and entry["template"] is not False:
            out.append(f"{eid}: template must be true or false")
        elif keys or "{" in url:
            out.append(f"{eid}: url has a {{placeholder}} but the entry is not a template")
    return out


def _feeds_of(doc: Any) -> Optional[List[Any]]:
    if isinstance(doc, dict) and isinstance(doc.get("feeds"), list):
        return list(doc["feeds"])
    return None


def merge_catalogs(shipped: Dict[str, Any], custom: Optional[Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(feeds, problems): the shipped entries in their order (a custom entry with the same id
    replaces one in place, `disabled: true` hides it), then the custom entries with new ids in
    file order. Every served entry carries `source`."""
    problems: List[str] = []
    order: List[str] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    for entry in _feeds_of(shipped) or []:
        bad = entry_problems(entry, shipped=True)
        if bad:
            problems.extend(f"shipped {p}" for p in bad)
            continue
        if entry["id"] in by_id:
            problems.append(f"shipped {entry['id']}: duplicate id")
            continue
        order.append(entry["id"])
        by_id[entry["id"]] = {**entry, "source": "shipped"}
    if custom is not None:
        feeds = _feeds_of(custom)
        if feeds is None:
            problems.append('custom catalogue: expected {"feeds": [...]}')
            feeds = []
        seen: set = set()
        for entry in feeds:
            eid = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(eid, str) and eid in seen:
                problems.append(f"custom {eid}: duplicate id (the first one is used)")
                continue
            if isinstance(entry, dict) and entry.get("disabled") is True:
                if isinstance(eid, str):
                    seen.add(eid)
                    by_id.pop(eid, None)
                continue
            bad = entry_problems(entry, shipped=False)
            if bad:
                problems.extend(f"custom {p}" for p in bad)
                continue
            seen.add(eid)
            row = {k: v for k, v in entry.items() if k != "disabled"}
            row.setdefault("category", DEFAULT_CUSTOM_CATEGORY)
            row["source"] = "custom"
            if eid not in order:
                order.append(eid)
            by_id[eid] = row
    return [by_id[i] for i in order if i in by_id], problems


def load_catalog(data_dir: Path) -> Dict[str, Any]:
    """The merged catalogue served to the console: `{version, feeds, custom_path, problems}`."""
    path = custom_catalog_path(data_dir)
    custom: Optional[Any] = None
    problems: List[str] = []
    if path.is_file():
        try:
            custom = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"custom catalogue {path} is not readable JSON: {exc}")
    feeds, merge_problems = merge_catalogs(shipped_catalog(), custom)
    return {"version": CATALOG_VERSION, "feeds": feeds, "custom_path": str(path), "problems": problems + merge_problems}
