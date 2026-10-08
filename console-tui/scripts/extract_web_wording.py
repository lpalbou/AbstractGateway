#!/usr/bin/env python3
"""Extract the web console's tooltip sentences of the two R15 reference screens.

The terminal console says the web console's words (DESIGN-TUI.md §8). The crate is published
alone, so `cargo test` (tests/r15_wording.rs) checks the terminal's tooltips against a copy:
`tests/fixtures/r15_web_wording.json`. THIS script rebuilds that copy from the live web sources
of the same repository and, by default, exits non-zero when the copy differs:

    python3 scripts/extract_web_wording.py           # check the fixture against the web
    python3 scripts/extract_web_wording.py --write   # regenerate the fixture

What it reads (each anchor is required; a missing file or anchor is a FAILURE, never a pass):

- `src/abstractgateway/console_workspaces.py`: the `ACCOUNT_TIPS` table (the Accounts row
  actions' tooltips), as {key: template} with `{n}` for the account.
- `src/abstractgateway/console.py`: the Accounts confirmation sentences (archive user/entity,
  suspend, deactivate, rotate) and the "Email for everyone" switches (label + description).
- `src/abstractgateway/console_ui.py`: every template literal holding `${name}` inside the app
  card (from `const APP_FIRST_RUN` to the card's settings gear), with `${name}` written `{n}`.
  Any other `${...}` expression stays as written; the test reads it as "any text".
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

CRATE = Path(__file__).resolve().parent.parent
REPO = CRATE.parent
WEB = REPO / "src" / "abstractgateway"
FIXTURE = CRATE / "tests" / "fixtures" / "r15_web_wording.json"


def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    sys.exit(f"extract_web_wording: {msg}")


def read(name: str) -> str:
    p = WEB / name
    if not p.is_file():
        fail(f"{p} not found (the web source is required; a missing file is a FAILURE).")
    return p.read_text(encoding="utf-8")


def account_tips() -> dict[str, str]:
    src = read("console_workspaces.py")
    m = re.search(r"const ACCOUNT_TIPS = \{\n(.*?)\n\s*\};", src, re.S)
    if not m:
        fail("no `const ACCOUNT_TIPS = { ... };` block in console_workspaces.py.")
    out = {}
    for line in m.group(1).splitlines():
        e = re.fullmatch(r"\s*(\w+): \(n\) => `([^`]*)`,?", line)
        if not e:
            fail(f"ACCOUNT_TIPS line not understood: {line!r}")
        out[e.group(1)] = e.group(2).replace("${n}", "{n}")
    if len(out) < 9:
        fail(f"ACCOUNT_TIPS has {len(out)} entries; expected at least 9.")
    return out


def app_tips() -> list[str]:
    src = read("console_ui.py")
    start = src.find("const APP_FIRST_RUN")
    end_anchor = 'data-af-tip="${esc(`${name} settings`)}"'
    end = src.find(end_anchor, start)
    if start < 0 or end < 0:
        fail("the app card anchors (`const APP_FIRST_RUN` … the settings gear's data-af-tip) are missing in console_ui.py.")
    region = src[start : end + len(end_anchor)]
    found = []
    for m in re.finditer(r"`([^`]*\$\{name\}[^`]*)`", region):
        t = m.group(1)
        if "<" in t or "data-" in t:
            continue  # markup between two template literals, not a sentence
        t = t.replace("${name}", "{n}")
        if t not in found:
            found.append(t)
    for must in ("Install the newest {n} terminal app", "Bring the {n} to the front on this computer", "{n} settings"):
        if must not in found:
            fail(f"expected app tooltip {must!r} not found in the app card; the anchors moved.")
    return found


def one(src: str, pattern: str, what: str) -> str:
    m = re.findall(pattern, src)
    if len(m) != 1:
        fail(f"{what}: expected exactly one match in console.py, found {len(m)} (the anchor moved).")
    return m[0]


def account_confirms() -> dict[str, str]:
    """The Accounts confirmation sentences ({n} = the account id)."""
    src = read("console.py")
    out = {
        "archive_user": one(src, r"user: \(id\) => `(Archive \$\{id\}\? They can't[^`]*)`", "archive (user)"),
        "archive_entity": one(src, r"entity: \(id\) => `(Archive \$\{id\}\? It stops acting[^`]*)`", "archive (entity)"),
        "suspend": one(src, r"`(Suspend \$\{a\.id\}\?[^`]*)`", "suspend"),
        "deactivate": one(src, r"`(Deactivate \$\{a\.id\}\?[^`]*)`", "deactivate"),
        "rotate": one(src, r"userConfirmRow\(tr, `(Rotate the token of \$\{a\.id\}\?[^`]*)`", "rotate"),
    }
    return {k: v.replace("${id}", "{n}").replace("${a.id}", "{n}") for k, v in out.items()}


def email_switches() -> list[dict[str, str]]:
    """The "Email for everyone" card's switches: label + description."""
    src = read("console.py")
    out = []
    for cap in ("email", "agent-tools", "recovery"):
        m = re.search(
            rf'id="email-cap-{cap}".*?<span class="af-switch__label">([^<]*)</span>'
            rf'<span class="af-switch__desc" id="email-cap-{cap}-desc">([^<]*)</span>',
            src,
        )
        if not m:
            fail(f"the email-cap-{cap} switch markup moved in console.py.")
        out.append({"label": html.unescape(m.group(1)), "desc": html.unescape(m.group(2))})
    return out


def build() -> dict:
    return {
        "_source": "scripts/extract_web_wording.py (do not edit by hand)",
        "account_tips": account_tips(),
        "app_tips": app_tips(),
        "account_confirms": account_confirms(),
        "email_switches": email_switches(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="regenerate the fixture")
    a = ap.parse_args()
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if a.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text, encoding="utf-8")
        print(f"wrote {FIXTURE}")
        return 0
    if not FIXTURE.is_file():
        fail(f"{FIXTURE} missing; run with --write.")
    if FIXTURE.read_text(encoding="utf-8") != text:
        print(f"{FIXTURE} differs from the web sources; run with --write, then make the terminal match (cargo test says where).")
        return 1
    print("r15 web wording: fixture matches the web sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
