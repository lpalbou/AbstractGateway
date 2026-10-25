#!/usr/bin/env python3
"""Re-verify the shipped feed catalogue live (network; never run by the test suite).

Every fixed feed is fetched and parsed exactly as the gateway's feed poller does it
(`abstractruntime.triggers.feed.urllib_fetch` + `parse_feed`) and must answer 200 with at least
one item that has a title and a link. A template (`"template": true`) is checked with its
placeholder's `example` value filled in.

    python scripts/check_feed_catalog.py                       # the shipped catalogue
    python scripts/check_feed_catalog.py path/to/feeds.json    # another catalogue (e.g. a custom one)
    python scripts/check_feed_catalog.py --log verify.log      # also write the log to a file

Exit status: 0 when every entry answers, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from abstractruntime.triggers.feed import FeedParseError, parse_feed, urllib_fetch


def _load(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.loads((resources.files("abstractgateway") / "assets" / "feeds.json").read_text(encoding="utf-8"))


def _address(entry: dict) -> str:
    url = str(entry["url"])
    if entry.get("template"):
        ph = entry["placeholder"]
        url = url.replace("{" + ph["key"] + "}", str(ph["example"]))
    return url


def check(entry: dict) -> tuple[bool, str]:
    url = _address(entry)
    started = time.monotonic()
    try:
        res = urllib_fetch(url)
    except Exception as exc:  # noqa: BLE001 - every failure is a log line
        return False, f"unreachable: {type(exc).__name__}: {exc}"
    ms = int((time.monotonic() - started) * 1000)
    if res.status != 200:
        return False, f"HTTP {res.status} ({ms} ms)"
    try:
        feed = parse_feed(res.body)
    except FeedParseError as exc:
        return False, f"unreadable: {exc} ({ms} ms)"
    good = [it for it in feed.items if it.title and it.link]
    if not good:
        return False, f"{feed.kind}: {len(feed.items)} items, none with a title and a link ({ms} ms)"
    first = good[0]
    return True, f"{feed.kind} {feed.title!r}: {len(feed.items)} items; first: {first.title[:70]!r} <{first.link[:90]}> ({ms} ms)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("catalog", nargs="?", help="a feeds.json (default: the shipped catalogue)")
    ap.add_argument("--log", help="also write the log to this file")
    args = ap.parse_args(argv)
    catalog = _load(args.catalog)
    lines = [f"# feed catalogue check {datetime.now(timezone.utc).isoformat(timespec='seconds')}"]
    failed = 0
    for entry in catalog.get("feeds", []):
        if entry.get("disabled"):
            continue
        ok, detail = check(entry)
        failed += 0 if ok else 1
        line = f"{'OK  ' if ok else 'FAIL'} {entry['id']:<24} {_address(entry)}\n     {detail}"
        lines.append(line)
        print(line, flush=True)
    summary = f"# {len(lines) - 1} checked, {failed} failed"
    lines.append(summary)
    print(summary)
    if args.log:
        Path(args.log).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
