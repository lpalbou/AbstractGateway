"""Probe a running benchmark gateway and measure it at rest (stdlib only; started by bench_gateway.sh).

Probes, in rounds (one call of each per round, sequential, like a client opening the apps):
  /api/gateway/me, /api/gateway/triggers/connections, /api/gateway/sessions/{id}/artifacts,
  /api/gateway/runs?limit=20, /api/gateway/automations
then an idle window with NO request: process CPU from cumulative CPU time (`ps -o time=`, not the decayed
`pcpu`), threads, RSS, garbage-collection pauses (from the netguard GC log) and outbound connections (lsof).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

PROBES = [
    ("me", "/api/gateway/me"),
    ("triggers_connections", "/api/gateway/triggers/connections"),
    ("session_artifacts", "/api/gateway/sessions/{sid}/artifacts"),
    ("runs_limit20", "/api/gateway/runs?limit=20"),
    ("automations", "/api/gateway/automations"),
]


def parse_ps_time(text: str) -> Optional[float]:
    """`ps -o time=` on macOS/Linux: [dd-][hh:]mm:ss[.ss] -> seconds."""
    s = (text or "").strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return days * 86400 + secs


def percentile(values: List[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (p in 0..100); None for no values."""
    if not values:
        return None
    vs = sorted(values)
    k = max(1, int(math.ceil(p / 100.0 * len(vs))))
    return vs[min(k, len(vs)) - 1]


def proc_cpu_s(pid: int) -> Optional[float]:
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout
    return parse_ps_time(out)


def proc_rss_mb(pid: int) -> Optional[int]:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(int(out) / 1024) if out.isdigit() else None


def proc_threads(pid: int) -> Optional[int]:
    out = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True).stdout
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    return len(lines) or None


def outbound_connections(pid: int) -> List[str]:
    """Established/connecting TCP/UDP endpoints of `pid` whose remote side is not loopback."""
    out = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-i"], capture_output=True, text=True).stdout
    bad = []
    for line in out.splitlines()[1:]:
        m = re.search(r"->(\[[^\]]+\]|[^:\s]+):", line)
        if not m:
            continue
        host = m.group(1).strip("[]")
        if host.startswith("127.") or host in ("::1", "localhost"):
            continue
        bad.append(line.strip())
    return bad


def probe(base: str, path: str, token: str, timeout: float) -> Tuple[float, int]:
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0  # timeout / connection error
    return time.perf_counter() - t0, code


def read_gc_log(path: str, t_from: float, t_to: float) -> List[Dict]:
    if not path or not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if t_from <= float(r.get("t", 0)) <= t_to:
                rows.append(r)
    return rows


def gc_summary(rows: List[Dict]) -> Dict:
    d = [float(r["dt_s"]) for r in rows]
    return {
        "pauses_ge_5ms": len(d),
        "pauses_ge_100ms": sum(1 for x in d if x >= 0.1),
        "max_s": max(d) if d else 0.0,
        "total_s": round(sum(d), 3),
        "by_gen": {str(g): sum(1 for r in rows if r.get("gen") == g) for g in (0, 1, 2)},
        "max_gen2_s": max((float(r["dt_s"]) for r in rows if r.get("gen") == 2), default=0.0),
    }


def fmt_s(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v * 1000:.0f} ms" if v < 1 else f"{v:.2f} s"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=1, help="untimed rounds first (caches, lazy indexes)")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--settle", type=float, default=15.0, help="seconds between the probes and the idle window")
    ap.add_argument("--idle", type=float, default=120.0)
    ap.add_argument("--gc-log", default="")
    ap.add_argument("--guard-log", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    t_start = time.time()
    meta = {"label": a.label, "pid": a.pid, "base": a.base, "session": a.session, "rounds": a.rounds,
            "warmup": a.warmup, "timeout_s": a.timeout, "idle_s": a.idle, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    for _ in range(max(0, a.warmup)):
        for _name, path in PROBES:
            probe(a.base, path.format(sid=a.session), a.token, a.timeout)
    samples: Dict[str, List[Tuple[float, int]]] = {n: [] for n, _ in PROBES}
    t_probe0, cpu_probe0 = time.time(), proc_cpu_s(a.pid)
    for _ in range(max(0, a.rounds)):
        for name, path in PROBES:
            samples[name].append(probe(a.base, path.format(sid=a.session), a.token, a.timeout))
    t_probe1, cpu_probe1 = time.time(), proc_cpu_s(a.pid)

    time.sleep(max(0.0, a.settle))
    outbound: List[str] = []
    threads: List[int] = []
    rss: List[int] = []
    t0, c0 = time.time(), proc_cpu_s(a.pid)
    end = t0 + a.idle
    while time.time() < end:
        outbound.extend(outbound_connections(a.pid))
        th, mb = proc_threads(a.pid), proc_rss_mb(a.pid)
        if th:
            threads.append(th)
        if mb:
            rss.append(mb)
        time.sleep(max(0.0, min(30.0, end - time.time())))
    t1, c1 = time.time(), proc_cpu_s(a.pid)

    probes_out = {}
    for name, rows in samples.items():
        ok = [t for t, c in rows if c == 200]
        allt = [t for t, _c in rows]
        probes_out[name] = {
            "n": len(rows), "ok": len(ok), "codes": sorted({c for _t, c in rows}),
            "p50_s": percentile(allt, 50), "p99_s": percentile(allt, 99), "max_s": max(allt) if allt else None,
        }
    idle_cpu = None
    if c0 is not None and c1 is not None and t1 > t0:
        idle_cpu = round((c1 - c0) * 100.0 / (t1 - t0), 1)
    probe_cpu = None
    if cpu_probe0 is not None and cpu_probe1 is not None and t_probe1 > t_probe0:
        probe_cpu = round((cpu_probe1 - cpu_probe0) * 100.0 / (t_probe1 - t_probe0), 1)
    guard_refusals = 0
    if a.guard_log and os.path.exists(a.guard_log):
        with open(a.guard_log, encoding="utf-8") as fh:
            guard_refusals = sum(1 for _ in fh)
    result = {
        **meta,
        "probes": probes_out,
        "probe_window": {"seconds": round(t_probe1 - t_probe0, 1), "cpu_pct": probe_cpu},
        "idle": {"seconds": round(t1 - t0, 1), "cpu_pct": idle_cpu, "cpu_s": None if c0 is None or c1 is None else round(c1 - c0, 2),
                 "threads_max": max(threads) if threads else None, "rss_mb_max": max(rss) if rss else None,
                 "gc": gc_summary(read_gc_log(a.gc_log, t0, t1))},
        "gc_whole_run": gc_summary(read_gc_log(a.gc_log, t_start, t1)),
        "network": {"outbound_connections_seen": sorted(set(outbound)), "guard_refusals_total": guard_refusals},
    }

    print(f"\nbenchmark {a.label}  (pid {a.pid}, {a.rounds} rounds after {a.warmup} warm-up, timeout {a.timeout:.0f} s)")
    print(f"{'probe':<24}{'p50':>10}{'p99':>10}{'max':>10}  ok/n  codes")
    for name, r in probes_out.items():
        print(f"{name:<24}{fmt_s(r['p50_s']):>10}{fmt_s(r['p99_s']):>10}{fmt_s(r['max_s']):>10}  {r['ok']}/{r['n']}  {r['codes']}")
    idle = result["idle"]
    g = idle["gc"]
    print(f"CPU during probes: {probe_cpu}% of a core over {result['probe_window']['seconds']} s")
    print(f"CPU at rest: {idle_cpu}% of a core over {idle['seconds']} s (cumulative CPU {idle['cpu_s']} s); "
          f"threads max {idle['threads_max']}, RSS max {idle['rss_mb_max']} MB")
    print(f"GC at rest: {g['pauses_ge_5ms']} pauses >= 5 ms, {g['pauses_ge_100ms']} >= 100 ms, longest {fmt_s(g['max_s'])}, "
          f"total {g['total_s']} s; whole run longest {fmt_s(result['gc_whole_run']['max_s'])}")
    print(f"network: outbound connections seen {len(result['network']['outbound_connections_seen'])}, "
          f"guard refusals {guard_refusals}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(f"json: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
