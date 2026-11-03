#!/bin/bash
# The gateway load test (backlog 1054), the same in CI (.github/workflows/perf.yml) and on a laptop:
# a synthetic fixture (10k runs, 100k artifacts, 50 automations; scripts/perf/make_fixture.py, no operator
# data), a hermetic gateway on it (scripts/perf/bench_gateway.sh: --no-tray, scratch HOME, empty
# environment, outbound network refused, paused), 20 probe rounds, then a 2-minute idle window.
#
# The limits: p99 /me 50 ms, /triggers/connections 100 ms, /sessions/{id}/artifacts 100 ms,
# /runs?limit=20 200 ms, /automations 500 ms, and CPU at rest over the idle window --cpu-max % of a core
# (default 5; 10 on CI runners). Without --enforce they are REPORTED: the numbers and any limit that was
# exceeded are printed (and written to the GitHub job summary when $GITHUB_STEP_SUMMARY is set) and the
# script exits 0. With --enforce an exceeded limit FAILS the run (exit 1). A probe that answered anything
# else than 200, a gateway that did not boot, or a missing result FAILS in both modes. Pull requests run
# it without --enforce; the nightly and manual runs pass --enforce (ADR 0050 rule 5: a wall-clock bound
# is a gate only where it is asked for explicitly, never on the default path).
#
#   scripts/perf/ci.sh [--enforce] [--work DIR] [--port 18080] [--cpu-max PCT] [--idle 120] [--rounds 20]
#                      [--python PY] [--runtime-src DIR|installed] [--gateway-src DIR|installed]
#                      [--runs 10000] [--artifacts 100000] [--automations 50] [--reuse-fixture]
#                      [--serve-arg ARG]...   # extra `serve` flags, e.g. --serve-arg --run-index --serve-arg trusted
#
# --work: fixture, logs, scratch HOME and the result JSON (default <repo>/untracked/perf-ci).
# --reuse-fixture: keep <work>/fixture when it exists (the gateway writes indexes and logs into it, so a
#   reused fixture is warmer than a fresh one; CI always builds it fresh).
# Defaults when run from the framework checkout: the framework venv and the sibling abstractruntime
# checkout; otherwise python3 and the installed packages.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
FRAMEWORK="$(cd "$REPO/.." && pwd)"
WORK="$REPO/untracked/perf-ci"
PORT=18080
IDLE=120; ROUNDS=20
RUNS=10000; ARTIFACTS=100000; AUTOMATIONS=50
REUSE=0
ENFORCE=0
SERVE_PASS=()
if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then CPU_MAX=10; else CPU_MAX=5; fi
PY="$FRAMEWORK/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
RT_SRC="$FRAMEWORK/abstractruntime/src"; [ -d "$RT_SRC/abstractruntime" ] || RT_SRC=installed
GW_SRC="$REPO/src"
while [ $# -gt 0 ]; do
  case "$1" in
    --work) WORK="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --cpu-max) CPU_MAX="$2"; shift 2 ;;
    --idle) IDLE="$2"; shift 2 ;;
    --rounds) ROUNDS="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --runtime-src) RT_SRC="$2"; shift 2 ;;
    --gateway-src) GW_SRC="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --artifacts) ARTIFACTS="$2"; shift 2 ;;
    --automations) AUTOMATIONS="$2"; shift 2 ;;
    --reuse-fixture) REUSE=1; shift ;;
    --enforce) ENFORCE=1; shift ;;
    --serve-arg) SERVE_PASS+=(--serve-arg "$2"); shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
FIXTURE="$WORK/fixture"
T0=$(date +%s)
# The fixture generator imports the runtime: the chosen checkout first, like the gateway will.
GEN_PATH=""
[ "$RT_SRC" = installed ] || GEN_PATH="$RT_SRC"
if [ "$REUSE" = 1 ] && [ -f "$FIXTURE/fixture.json" ]; then
  echo "reusing the fixture at $FIXTURE"
else
  echo "building the fixture: $RUNS runs, $ARTIFACTS artifacts, $AUTOMATIONS automations -> $FIXTURE"
  PYTHONPATH="$GEN_PATH" "$PY" "$HERE/make_fixture.py" --dest "$FIXTURE" --runs "$RUNS" --artifacts "$ARTIFACTS" \
    --automations "$AUTOMATIONS" --replace
fi
SESSION="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["session"])' "$FIXTURE/fixture.json")"
T_FIX=$(( $(date +%s) - T0 ))
echo "fixture ready in ${T_FIX} s (session $SESSION); CPU-at-rest limit ${CPU_MAX}% of a core; limits $([ "$ENFORCE" = 1 ] && echo ENFORCED || echo "reported (no --enforce)")"

RC=0
"$HERE/bench_gateway.sh" --clone "$FIXTURE" --session "$SESSION" --port "$PORT" --python "$PY" \
  --runtime-src "$RT_SRC" --gateway-src "$GW_SRC" --perf-dir "$WORK" --rounds "$ROUNDS" --warmup 1 --idle "$IDLE" \
  --timeout 30 --boot-timeout 300 --json --label "ci-$(date +%Y%m%d-%H%M%S)" \
  --driver-arg --fail-p99 --driver-arg me=0.05 \
  --driver-arg --fail-p99 --driver-arg triggers_connections=0.1 \
  --driver-arg --fail-p99 --driver-arg session_artifacts=0.1 \
  --driver-arg --fail-p99 --driver-arg runs_limit20=0.2 \
  --driver-arg --fail-p99 --driver-arg automations=0.5 \
  --driver-arg --fail-idle-cpu --driver-arg "$CPU_MAX" ${SERVE_PASS[@]+"${SERVE_PASS[@]}"} || RC=$?
# The result of THIS run (bench_gateway.sh --json writes <work>/bench-<timestamp>.json), then the verdict:
# exceeded limits fail only with --enforce; non-200 answers, a missing result or a boot failure always fail.
VERDICT="$("$PY" -I - "$WORK" "$T0" "$RC" "$ENFORCE" <<'PY'
import glob, json, os, sys
work, t0, rc, enforce = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "1"
runs = [p for p in glob.glob(os.path.join(work, "bench-*.json")) if os.path.getmtime(p) >= t0]
doc = json.load(open(max(runs, key=os.path.getmtime))) if runs else None
gates = (doc or {}).get("gates") or {}
failures = list(gates.get("failures") or [])
correctness = [f for f in failures if "were not 200" in f]
limits = [f for f in failures if f not in correctness]
if doc is None or (rc != 0 and not failures):
    verdict, code = "FAIL: no result (the gateway did not boot or the driver failed; see the log above)", 1
elif correctness:
    verdict, code = "FAIL: " + "; ".join(correctness), 1
elif limits and enforce:
    verdict, code = "FAIL (limits enforced): " + "; ".join(limits), 1
elif limits:
    verdict, code = "PASS (limits reported, not enforced): over " + "; ".join(limits), 0
else:
    verdict, code = "PASS", 0
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary and doc is not None:
    bounds = gates.get("p99_s") or {}
    ms = lambda v: "-" if v is None else f"{v * 1000:.1f} ms"
    lines = ["## Gateway load test", "", f"Mode: {'limits enforced (--enforce)' if enforce else 'limits reported (pull request)'}. {verdict}", "",
             "| Probe | p50 | p99 | p99 limit | max | 200s |", "|---|---|---|---|---|---|"]
    for name, r in (doc.get("probes") or {}).items():
        lines.append(f"| {name} | {ms(r.get('p50_s'))} | {ms(r.get('p99_s'))} | {ms(bounds.get(name))} | {ms(r.get('max_s'))} | {r.get('ok')}/{r.get('n')} |")
    idle = doc.get("idle") or {}
    lines += ["", f"CPU at rest: {idle.get('cpu_pct')}% of a core over {idle.get('seconds')} s (limit {gates.get('idle_cpu_pct')}%).", ""]
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
print(f"{code} {verdict}")
PY
)" || VERDICT="1 FAIL: the verdict could not be computed"
CODE="${VERDICT%% *}"
echo "load test: ${VERDICT#* } in $(( $(date +%s) - T0 )) s; results under $WORK"
exit "$CODE"
