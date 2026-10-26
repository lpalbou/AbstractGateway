#!/bin/bash
# Hermetic benchmark gateway on the scrubbed clone (scripts/perf/clone_runtime.sh), then probes + an idle window.
#
#   scripts/perf/bench_gateway.sh [--json] [--label TEXT] [--rounds 20] [--idle 120] [--warmup 1] [--timeout 120]
#                                 [--port 18080] [--clone DIR] [--session ID]
#                                 [--gateway-src DIR] [--runtime-src DIR]   # code to run (default: the checkouts)
#                                 [--serve-arg ARG]...                       # extra `serve` flags, e.g. --gc-tuning off
#
# The gateway: 127.0.0.1:<port>, --no-tray, --exec-runner off, --push-relay off, an EMPTY environment (env -i) with a
# scratch HOME/TMPDIR/HF_HOME under untracked/perf/bench-home (no keychain, no ~/.abstractcore, no provider keys),
# a random admin token, the clone paused (gateway_paused.json: the runner loop and its store probes run, no tick is
# scheduled), and scripts/perf/netguard/sitecustomize.py on PYTHONPATH: it refuses every non-loopback connect and
# name lookup (logged) and logs every garbage collection >= 5 ms. lsof checks for outbound connections during the
# idle window. The gateway is stopped on every exit path (trap). Output: a table; --json also writes
# untracked/perf/bench-<timestamp>.json. Logs: untracked/perf/bench-logs/<timestamp>/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
FRAMEWORK="$(cd "$REPO/.." && pwd)"
PERF="$FRAMEWORK/untracked/perf"
VENV_PY="$FRAMEWORK/.venv/bin/python"
CLONE="$PERF/runtime-clone"
PORT=18080
ROUNDS=20; IDLE=120; WARMUP=1; TIMEOUT=120; BOOT_TIMEOUT=1200
JSON=0; LABEL=""
SESSION="6529fb2d-573b-4521-b0e1-d9adbae4f275"
GW_SRC="$REPO/src"; RT_SRC="$FRAMEWORK/abstractruntime/src"
SERVE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --label) LABEL="$2"; shift 2 ;;
    --rounds) ROUNDS="$2"; shift 2 ;;
    --idle) IDLE="$2"; shift 2 ;;
    --warmup) WARMUP="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --boot-timeout) BOOT_TIMEOUT="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --clone) CLONE="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    --gateway-src) GW_SRC="$2"; shift 2 ;;
    --runtime-src) RT_SRC="$2"; shift 2 ;;
    --serve-arg) SERVE_ARGS+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$PORT" != 8080 ] || { echo "refusing port 8080: that is the operator's gateway" >&2; exit 2; }
[ -f "$CLONE/README-scrub.md" ] || { echo "no scrubbed clone at $CLONE: run scripts/perf/clone_runtime.sh first" >&2; exit 2; }
[ -f "$CLONE/gateway_paused.json" ] || { echo "the clone is not paused ($CLONE/gateway_paused.json missing)" >&2; exit 2; }
[ -f "$CLONE/run_$SESSION.json" ] || { echo "session $SESSION has no run file in the clone" >&2; exit 2; }
case "$(cd "$CLONE" && pwd -P)" in "$(cd "$FRAMEWORK/runtime" 2>/dev/null && pwd -P)") echo "refusing: --clone is the live data dir" >&2; exit 2 ;; esac
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then echo "port $PORT is already in use" >&2; exit 2; fi
[ -d "$GW_SRC/abstractgateway" ] || { echo "no abstractgateway package under $GW_SRC" >&2; exit 2; }
[ -d "$RT_SRC/abstractruntime" ] || { echo "no abstractruntime package under $RT_SRC" >&2; exit 2; }

TS="$(date +%Y%m%d-%H%M%S)"
LOGS="$PERF/bench-logs/$TS"
HOME_DIR="$PERF/bench-home"
mkdir -p "$LOGS" "$HOME_DIR/tmp" "$HOME_DIR/hf"
GUARD_LOG="$LOGS/netguard.jsonl"; GC_LOG="$LOGS/gc.jsonl"; GW_LOG="$LOGS/gateway.log"
: > "$GUARD_LOG"; : > "$GC_LOG"
TOKEN="$(openssl rand -hex 24)"
FLOWS="$(cd "$GW_SRC/.." && pwd)/flows/bundles/"
# The live gateway's PYTHONPATH, with the guard and the chosen gateway/runtime code first.
PYPATH="$HERE/netguard:$GW_SRC:$RT_SRC"
for p in abstractmemory/src abstractflow/src abstractcamera/src abstract3d/src abstractagent/src abstractcore; do
  PYPATH="$PYPATH:$FRAMEWORK/$p"
done

GW_PID=""
cleanup() {
  local rc=$?
  if [ -n "$GW_PID" ] && kill -0 "$GW_PID" 2>/dev/null; then
    echo "stopping the benchmark gateway (pid $GW_PID)"
    kill -TERM "$GW_PID" 2>/dev/null || true
    for _ in $(seq 1 90); do kill -0 "$GW_PID" 2>/dev/null || break; sleep 1; done
    kill -0 "$GW_PID" 2>/dev/null && { echo "still alive after 90 s: SIGKILL"; kill -KILL "$GW_PID" 2>/dev/null || true; }
  fi
  # Anything still holding the port or naming the clone as its data dir is ours.
  local left
  left="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  [ -n "$left" ] && { echo "killing leftover listener(s) on $PORT: $left"; kill -KILL $left 2>/dev/null || true; }
  pkill -KILL -f -- "--data-dir $CLONE" 2>/dev/null || true
  exit $rc
}
trap cleanup EXIT INT TERM

echo "starting the benchmark gateway: $GW_SRC + $RT_SRC on 127.0.0.1:$PORT, data dir $CLONE ${SERVE_ARGS[*]:-}"
env -i HOME="$HOME_DIR" USER="$(id -un)" LOGNAME="$(id -un)" PATH="/usr/bin:/bin:/usr/sbin:/sbin" LANG=en_US.UTF-8 \
  TMPDIR="$HOME_DIR/tmp/" HF_HOME="$HOME_DIR/hf" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ABSTRACTGATEWAY_USER_AUTH=1 ABSTRACTGATEWAY_AUTH_TOKEN="$TOKEN" ABSTRACTGATEWAY_FLOWS_DIR="$FLOWS" \
  PYTHONPATH="$PYPATH" PERF_GUARD_LOG="$GUARD_LOG" PERF_GC_LOG="$GC_LOG" \
  "$VENV_PY" -P -m abstractgateway serve --host 127.0.0.1 --port "$PORT" --data-dir "$CLONE" \
  --no-tray --exec-runner off --push-relay off --no-print-token ${SERVE_ARGS[@]+"${SERVE_ARGS[@]}"} \
  >"$GW_LOG" 2>&1 &
GW_PID=$!
echo "gateway pid $GW_PID, log $GW_LOG"

T0=$(date +%s)
while :; do
  kill -0 "$GW_PID" 2>/dev/null || { echo "the gateway exited during boot; last log lines:" >&2; tail -30 "$GW_LOG" >&2; exit 1; }
  H="$(curl -s -m 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
  if [ -n "$H" ] && printf '%s' "$H" | /usr/bin/python3 -I -c '
import json, sys
d = json.load(sys.stdin)
r = d.get("runner") or {}
ok = d.get("status") in ("healthy", "degraded") and r.get("initialized") and not d.get("warming_up")
sys.exit(0 if ok else 1)'; then break; fi
  [ $(( $(date +%s) - T0 )) -lt "$BOOT_TIMEOUT" ] || { echo "boot did not settle within $BOOT_TIMEOUT s" >&2; exit 1; }
  sleep 3
done
BOOT_S=$(( $(date +%s) - T0 ))
LISTENER="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
[ "$LISTENER" = "$GW_PID" ] || { echo "the listener on $PORT is pid $LISTENER, not $GW_PID" >&2; exit 1; }
echo "boot settled in ${BOOT_S} s"
# The GC tuning line (if the code has it) is printed a little after the boot settles.
sleep 5
grep -E "\[INFO\] gc:" "$GW_LOG" || echo "(no gc tuning line in the log: this code has no --gc-tuning, or it is still pending)"

JSON_PATH=""
[ "$JSON" = 1 ] && JSON_PATH="$PERF/bench-$TS.json"
/usr/bin/python3 -I "$HERE/bench_driver.py" --base "http://127.0.0.1:$PORT" --token "$TOKEN" --pid "$GW_PID" \
  --session "$SESSION" --rounds "$ROUNDS" --warmup "$WARMUP" --timeout "$TIMEOUT" --idle "$IDLE" \
  --gc-log "$GC_LOG" --guard-log "$GUARD_LOG" --label "${LABEL:-$TS}" ${JSON_PATH:+--json "$JSON_PATH"}
if [ -n "$JSON_PATH" ]; then
  /usr/bin/python3 -I - "$JSON_PATH" "$BOOT_S" "$GW_SRC" "$RT_SRC" "$GW_LOG" "${SERVE_ARGS[*]:-}" <<'PYEOF'
import json, subprocess, sys
path, boot_s, gw, rt, log, serve_args = sys.argv[1:7]
d = json.load(open(path))
def head(src):
    try:
        return subprocess.run(["git", "-C", src, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        return None
def dirty(src):
    try:
        return bool(subprocess.run(["git", "-C", src, "status", "--porcelain", "--", "."], capture_output=True, text=True).stdout.strip())
    except Exception:
        return None
d["boot_s"] = int(boot_s)
d["code"] = {"gateway_src": gw, "gateway_head": head(gw), "gateway_dirty": dirty(gw),
             "runtime_src": rt, "runtime_head": head(rt), "runtime_dirty": dirty(rt), "serve_args": serve_args}
d["gc_tuning_log"] = [l.strip() for l in open(log, errors="replace") if "[INFO] gc:" in l]
json.dump(d, open(path, "w"), indent=2)
PYEOF
fi
echo "guard refusals (first 5):"; head -5 "$GUARD_LOG" || true
