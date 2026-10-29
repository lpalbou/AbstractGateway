#!/usr/bin/env python3
"""Split the test files into N balanced CI shards (pull requests run them as parallel jobs).

Balancing uses `ci/test_durations.json` (seconds per test file, from a full run:
`python scripts/ci_shard.py --record junit.xml`); a file it does not know yet counts as the median.
Each file lands in exactly one shard (whole files, never single tests: a file's tests share
fixtures and module state, exactly as in a serial run). The assignment is deterministic, so a
re-run of one shard runs the same files.

    python scripts/ci_shard.py --shards 4 --index 0        # the files of shard 0, one per line
    python scripts/ci_shard.py --shards 4 --plan           # every shard with its expected seconds
    python scripts/ci_shard.py --record junit-*.xml       # rewrite ci/test_durations.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = ROOT / "ci" / "test_durations.json"


def test_files(root: Path = ROOT) -> List[str]:
    """What pytest collects (`testpaths = ["tests"]`, `python_files = ["test_*.py"]`)."""
    return sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))


def load_durations(path: Path = DURATIONS) -> Dict[str, float]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in (doc.get("files") or {}).items()}


def plan(n: int, files: List[str], durations: Dict[str, float]) -> List[List[str]]:
    known = [durations[f] for f in files if f in durations]
    default = statistics.median(known) if known else 1.0
    weighted = sorted(files, key=lambda f: (-durations.get(f, default), f))
    shards: List[List[str]] = [[] for _ in range(n)]
    totals = [0.0] * n
    for f in weighted:  # greedy: the longest file goes to the lightest shard
        i = min(range(n), key=lambda k: (totals[k], k))
        shards[i].append(f)
        totals[i] += durations.get(f, default)
    return [sorted(s) for s in shards]


def record(junits: List[Path], out: Path = DURATIONS) -> Dict[str, float]:
    per_file: Dict[str, float] = {}
    cases = [c for j in junits for c in ET.parse(j).getroot().iter("testcase")]
    for case in cases:
        file = case.get("file")
        if not file:  # classname is `tests.test_x` or `tests.test_x.TestClass`: the longest file prefix
            parts = (case.get("classname") or "").split(".")
            for k in range(len(parts), 0, -1):
                candidate = "/".join(parts[:k]) + ".py"
                if (ROOT / candidate).is_file():
                    file = candidate
                    break
        if not file:
            continue
        per_file[file] = per_file.get(file, 0.0) + float(case.get("time") or 0.0)
    doc = {"note": "seconds per test file from a full local run; scripts/ci_shard.py balances CI shards with it",
           "files": {k: round(v, 2) for k, v in sorted(per_file.items())}}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return per_file


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--index", type=int)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--record", type=Path, nargs="+", help="junit XML files of a full run (every shard)")
    args = ap.parse_args(argv)
    if args.record:
        per = record(args.record)
        print(f"recorded {len(per)} files, {sum(per.values()):.0f} s in all -> {DURATIONS.relative_to(ROOT)}")
        return 0
    files, durations = test_files(), load_durations()
    shards = plan(args.shards, files, durations)
    if args.plan:
        known = [durations[f] for f in files if f in durations]
        default = statistics.median(known) if known else 1.0
        for i, s in enumerate(shards):
            print(f"shard {i}: {len(s)} files, ~{sum(durations.get(f, default) for f in s):.0f} s")
        return 0
    if args.index is None or not 0 <= args.index < args.shards:
        ap.error("--index must be in [0, --shards)")
    sys.stdout.write("\n".join(shards[args.index]) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
