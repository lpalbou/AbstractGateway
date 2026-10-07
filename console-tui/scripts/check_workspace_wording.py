#!/usr/bin/env python3
"""Diff the terminal console's workspace wording against the kit's ONE wording table.

The kit (abstractuic/ui-kit/src/workspace_chooser_core.ts, `WORKSPACE_CHOOSER_TEXT`) is the source
of every workspace word on every surface (DESIGN R11.1 FINAL). The console crate cannot read the
kit at build time (it is published alone), so it carries a byte-for-byte copy in two places:

- `src/ui/workspace_chooser.rs` (`TABLE`, what the screens print), and
- `tests/fixtures/r14w3_kit_workspace_chooser_text.json` (the kit's table as `[[key, value], ...]`).

`cargo test` diffs the Rust table against the JSON fixture (always). THIS script diffs the JSON
fixture against the live kit file and exits non-zero on any difference, or when the kit file
cannot be found (a missing kit is a failure, never a pass).

    python3 scripts/check_workspace_wording.py [--kit <path to workspace_chooser_core.ts>] [--write]

Default kit path: <gateway repo>/../abstractuic/ui-kit/src/workspace_chooser_core.ts, else
$ABSTRACTUIC_DIR/ui-kit/src/workspace_chooser_core.ts. `--write` regenerates the fixture from the kit
(then update `TABLE` in the Rust module to match; `cargo test` says where).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CRATE = HERE.parent
FIXTURE = CRATE / "tests" / "fixtures" / "r14w3_kit_workspace_chooser_text.json"
REL = Path("ui-kit") / "src" / "workspace_chooser_core.ts"


def kit_path(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("ABSTRACTUIC_DIR")
    candidates = []
    if env:
        candidates.append(Path(env).expanduser() / REL)
    # <framework>/abstractgateway/console-tui -> <framework>/abstractuic
    candidates.append(CRATE.parent.parent / "abstractuic" / REL)
    for c in candidates:
        if c.is_file():
            return c.resolve()
    sys.exit(
        "check_workspace_wording: the kit table was not found (tried: "
        + ", ".join(str(c) for c in candidates)
        + "). Pass --kit <path>/workspace_chooser_core.ts or set ABSTRACTUIC_DIR. A missing kit is a FAILURE."
    )


def kit_table(path: Path) -> list[list[str]]:
    src = path.read_text(encoding="utf-8")
    m = re.search(r"export const WORKSPACE_CHOOSER_TEXT = \{\n(.*?)\n\} as const;", src, re.S)
    if not m:
        sys.exit(f"check_workspace_wording: no `export const WORKSPACE_CHOOSER_TEXT = {{ ... }} as const;` block in {path}.")
    rows = []
    for line in m.group(1).splitlines():
        lm = re.fullmatch(r'\s*([A-Za-z0-9_]+): (".*"),', line)
        if not lm:
            sys.exit(f"check_workspace_wording: unexpected line in the kit table ({path}): {line!r} (one key per line, double-quoted).")
        rows.append([lm.group(1), json.loads(lm.group(2))])
    if not rows:
        sys.exit(f"check_workspace_wording: the kit table in {path} is empty.")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    path = kit_path(a.kit)
    kit = kit_table(path)
    if a.write:
        FIXTURE.write_text(json.dumps(kit, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {FIXTURE} ({len(kit)} keys) from {path}")
        return 0
    if not FIXTURE.is_file():
        print(f"check_workspace_wording: {FIXTURE} is missing.", file=sys.stderr)
        return 1
    ours = json.loads(FIXTURE.read_text(encoding="utf-8"))
    diffs = []
    kd, od = dict((k, v) for k, v in kit), dict((k, v) for k, v in ours)
    for k, v in kit:
        if k not in od:
            diffs.append(f"missing in the console: {k} = {v!r}")
        elif od[k] != v:
            diffs.append(f"differs: {k}: kit {v!r} != console {od[k]!r}")
    for k, v in ours:
        if k not in kd:
            diffs.append(f"not in the kit: {k} = {v!r}")
    if [k for k, _ in kit] != [k for k, _ in ours] and not diffs:
        diffs.append("same keys, different order")
    if diffs:
        print(f"check_workspace_wording: {len(diffs)} difference(s) between {path} and {FIXTURE}:", file=sys.stderr)
        for d in diffs:
            print("  " + d, file=sys.stderr)
        return 1
    print(f"check_workspace_wording: OK — {len(kit)} keys byte-identical ({path} vs {FIXTURE.name}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
