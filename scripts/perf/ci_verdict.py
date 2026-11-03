"""The load test's verdict (scripts/perf/ci.sh), from the recorded numbers only.

`bench_driver.py --json` writes one result document per run. The verdict reads its numbers, never
its printed text:

- correctness, which fails in both modes: no result, a probe with an answer other than 200
  (`ok < n`), a probe with no p99, or a CPU-at-rest limit with no measurement (`idle.cpu_pct` None);
- limits, which fail only with --enforce: a probe's p99 above its bound (`gates.p99_s`), CPU at rest
  above `gates.idle_cpu_pct`.

    python ci_verdict.py WORK T0 RC ENFORCE   ->  prints "<exit code> <sentence>"; writes the GitHub job
                                                  summary when $GITHUB_STEP_SUMMARY is set
"""

from __future__ import annotations

import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


def _ms(value: Any) -> str:
    return "-" if not isinstance(value, (int, float)) else f"{value * 1000:.1f} ms"


def problems(doc: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(correctness failures, exceeded limits) of one result document."""
    gates = doc.get("gates") if isinstance(doc.get("gates"), dict) else {}
    bounds = gates.get("p99_s") if isinstance(gates.get("p99_s"), dict) else {}
    probes = doc.get("probes") if isinstance(doc.get("probes"), dict) else {}
    correctness: List[str] = []
    limits: List[str] = []
    for name, bound in bounds.items():
        r = probes.get(name)
        if not isinstance(r, dict):
            correctness.append(f"{name}: no measurement")
            continue
        n, ok = r.get("n"), r.get("ok")
        if not isinstance(n, int) or not isinstance(ok, int) or n == 0 or ok != n:
            correctness.append(f"{name}: {ok} of {n} answers were 200 (codes {r.get('codes')})")
        p99 = r.get("p99_s")
        if not isinstance(p99, (int, float)):
            correctness.append(f"{name}: no p99")
        elif p99 > bound:
            limits.append(f"{name}: p99 {_ms(p99)} > {_ms(bound)}")
    cpu_limit = gates.get("idle_cpu_pct")
    if isinstance(cpu_limit, (int, float)):
        idle = doc.get("idle") if isinstance(doc.get("idle"), dict) else {}
        cpu = idle.get("cpu_pct")
        if not isinstance(cpu, (int, float)):
            correctness.append("CPU at rest: not measured")
        elif cpu > cpu_limit:
            limits.append(f"CPU at rest {cpu}% > {cpu_limit}% of a core")
    return correctness, limits


def verdict(doc: Optional[Dict[str, Any]], rc: int, enforce: bool) -> Tuple[int, str]:
    if doc is None:
        return 1, "FAIL: no result (the gateway did not boot or the driver failed; see the log above)"
    if not isinstance(doc.get("gates"), dict):
        return 1, f"FAIL: the result carries no gates (driver exit {rc})"
    correctness, limits = problems(doc)
    if correctness:
        return 1, "FAIL: " + "; ".join(correctness)
    if limits and enforce:
        return 1, "FAIL (limits enforced): " + "; ".join(limits)
    if limits:
        return 0, "PASS (limits reported, not enforced): over " + "; ".join(limits)
    return 0, "PASS"


def summary_markdown(doc: Dict[str, Any], enforce: bool, sentence: str) -> str:
    gates = doc.get("gates") if isinstance(doc.get("gates"), dict) else {}
    bounds = gates.get("p99_s") if isinstance(gates.get("p99_s"), dict) else {}
    lines = ["## Gateway load test", "",
             f"Mode: {'limits enforced (--enforce)' if enforce else 'limits reported (pull request)'}. {sentence}", "",
             "| Probe | p50 | p99 | p99 limit | max | 200s |", "|---|---|---|---|---|---|"]
    for name, r in (doc.get("probes") or {}).items():
        lines.append(f"| {name} | {_ms(r.get('p50_s'))} | {_ms(r.get('p99_s'))} | {_ms(bounds.get(name))} | {_ms(r.get('max_s'))} | {r.get('ok')}/{r.get('n')} |")
    idle = doc.get("idle") or {}
    lines += ["", f"CPU at rest: {idle.get('cpu_pct')}% of a core over {idle.get('seconds')} s (limit {gates.get('idle_cpu_pct')}%).", ""]
    return "\n".join(lines) + "\n"


def main(argv: List[str]) -> int:
    work, t0, rc, enforce = argv[0], float(argv[1]), int(argv[2]), argv[3] == "1"
    runs = [p for p in glob.glob(os.path.join(work, "bench-*.json")) if os.path.getmtime(p) >= t0]
    doc = None
    if runs:
        with open(max(runs, key=os.path.getmtime), encoding="utf-8") as fh:
            doc = json.load(fh)
    code, sentence = verdict(doc, rc, enforce)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out and doc is not None:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(doc, enforce, sentence))
    print(f"{code} {sentence}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
