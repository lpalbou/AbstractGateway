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
# else than 200, a missing p99 or CPU measurement, a gateway that did not boot, or a missing result FAILS in
# both modes. The verdict reads the result's numbers (scripts/perf/ci_verdict.py), never printed text. Pull requests run
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
# The verdict from THIS run's result document (bench_gateway.sh --json writes <work>/bench-<timestamp>.json),
# read by numbers (scripts/perf/ci_verdict.py): exceeded limits fail only with --enforce; a non-200 answer,
# a missing p99 or CPU measurement, a missing result or a boot failure always fail.
VERDICT="$("$PY" -I "$HERE/ci_verdict.py" "$WORK" "$T0" "$RC" "$ENFORCE")" || VERDICT="1 FAIL: the verdict could not be computed"
CODE="${VERDICT%% *}"
echo "load test: ${VERDICT#* } in $(( $(date +%s) - T0 )) s; results under $WORK"
exit "$CODE"
