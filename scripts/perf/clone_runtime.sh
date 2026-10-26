#!/bin/bash
# Benchmark fixture: an APFS copy-on-write clone of a gateway data dir, scrubbed of every credential.
#
#   scripts/perf/clone_runtime.sh [--src DIR] [--dest DIR] [--replace]
#
# Defaults: --src <framework>/runtime, --dest <framework>/untracked/perf/runtime-clone (<framework> = the folder
# that holds this repo). The source is only READ (`cp -c -R`, a clone: no data block is copied, no extra disk until
# the copy is written). Every write of this script goes through a guard that refuses any path outside --dest.
#
# Left out of the clone: apps/ (installed browser apps: a gateway on the clone would reap or start them on the
# live gateway's ports) and dev/ (a Chrome profile, dev tokens). Removed from the clone, then listed in
# <dest>/README-scrub.md: sealed-secret key and sealed stores, mail accounts and outboxes (every plane), push
# devices/owners/settings (every plane), auth sessions/secrets/recovery codes/bootstrap token, named API keys and
# their sealed copies, account token hashes, MCP servers and their secrets, the /v1 endpoint token, config backups
# holding provider keys, pid/launch records (run/), the workflow-policy and entity stamp secrets. Provider API keys
# themselves live in the environment and in ~/.abstractcore: the benchmark gateway starts with a scratch HOME and
# an empty environment, so it never sees them.
#
# Proof that the source is untouched: the SHA-256 of a fixed set of source files (secrets and settings whose clone
# copies are removed or edited; the live gateway does not rewrite them) before and after; every path removed from
# the clone still present in the source; and `find <src> -newer <marker>` printed (a LIVE gateway on the source
# writes its own heartbeats, logs and watcher state there: listed, so a reader sees what changed and why).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAMEWORK="$(cd "$HERE/../../.." && pwd)"
SRC="$FRAMEWORK/runtime"
DEST="$FRAMEWORK/untracked/perf/runtime-clone"
REPLACE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
PY=/usr/bin/python3
[ -d "$SRC" ] || { echo "source not found: $SRC" >&2; exit 2; }
SRC="$(cd "$SRC" && pwd -P)"
mkdir -p "$(dirname "$DEST")"
DEST="$(cd "$(dirname "$DEST")" && pwd -P)/$(basename "$DEST")"
case "$DEST/" in "$SRC/"*) echo "refusing: the clone would be inside the source" >&2; exit 2 ;; esac
case "$SRC/" in "$DEST/"*) echo "refusing: the source is inside the clone" >&2; exit 2 ;; esac
if [ -e "$DEST" ]; then
  [ "$REPLACE" = 1 ] || { echo "$DEST exists: pass --replace to rebuild it" >&2; exit 2; }
  "$PY" -I -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$DEST"
fi

STAMP_DIR="$(dirname "$DEST")"
MARKER="$STAMP_DIR/.clone-marker"
SUMS_BEFORE="$STAMP_DIR/.clone-sums-before"
SUMS_AFTER="$STAMP_DIR/.clone-sums-after"
# Source files whose clone copies are scrubbed: their source copies must hash the same afterwards.
PROBES=(secrets/sealing.key auth/users.json auth/recovery.key auth/session_secret push/devices.json
        push/token_owners.json email/account/email/secret.enc config/core_endpoint.json config/mcp_secrets/secret.enc
        auth/openai_key_secrets/secret.enc users/default/laurent/email/account/email/secret.enc .workflow_policy_secret
        config/runtime_config.json entities/.stamp_secret)
sums() { (cd "$SRC" && for p in "${PROBES[@]}"; do if [ -f "$p" ]; then shasum -a 256 "$p"; else echo "absent  $p"; fi; done); }
sums > "$SUMS_BEFORE"
touch "$MARKER"
sleep 1

echo "cloning $SRC -> $DEST (APFS clone, apps/ and dev/ left out)"
mkdir "$DEST"
T0=$(date +%s)
CP_ERR="$STAMP_DIR/.clone-cp-errors"
: > "$CP_ERR"
shopt -s dotglob nullglob
for entry in "$SRC"/*; do
  name="$(basename "$entry")"
  case "$name" in apps|dev) continue ;; esac
  # A live gateway replaces files while we copy (tmp + rename): a vanished file is reported, not fatal.
  cp -c -R -p "$entry" "$DEST/" 2>>"$CP_ERR" || true
done
shopt -u dotglob nullglob
echo "cloned in $(( $(date +%s) - T0 )) s; cp warnings: $(wc -l < "$CP_ERR" | tr -d ' ') (see $CP_ERR)"

"$PY" -I - "$DEST" "$SRC" <<'PYEOF'
import json, os, shutil, sys, time
from pathlib import Path

dest = Path(sys.argv[1]).resolve()
src = sys.argv[2]
removed: list[str] = []
edited: list[str] = []


def inside(p: Path) -> Path:
    rp = Path(os.path.realpath(p))
    if rp != dest and dest not in rp.parents:
        raise SystemExit(f"GUARD: refusing to write outside the clone: {rp}")
    return rp


def rm(rel: str) -> None:
    p = dest / rel
    if not (p.exists() or p.is_symlink()):
        return
    if p.is_symlink():
        inside(p.parent)
        p.unlink()
    else:
        inside(p)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    removed.append(rel)


def planes() -> list[str]:
    """Every data-dir-like root in the clone: the admin plane, user planes, entity homes."""
    out = [""]
    for parent in ("users", "entities"):
        base = dest / parent
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_dir() and (p / "email").is_dir() or (p.is_dir() and (p / "push").is_dir()):
                out.append(str(p.relative_to(dest)))
    return out


# 1. The sealed-secret key: without it no sealed store in the clone can be opened, whatever is left.
rm("secrets")
# 2. Auth: browser sessions (bearer cookies), session/recovery secrets, bootstrap token, named API keys.
for rel in ("auth/sessions.json", "auth/session_secret", "auth/recovery.key", "auth/recovery_codes.json",
            "auth/bootstrap-admin-token", "auth/claims", "auth/openai_key_secrets", "auth/openai_key_usage.json",
            "auth/openai_key_secrets_moved.json", "auth/pairing", "handover"):
    rm(rel)
# 3. Mail accounts, sealed mail secrets and outboxes, on every plane; push devices and settings on every plane.
for plane in planes():
    pre = f"{plane}/" if plane else ""
    for rel in ("email/accounts", "email/account", "email/outbox.sqlite3", "email/outbox.sqlite3-wal",
                "email/outbox.sqlite3-shm", "email/core_import.json", "email/secret_key_migration.json",
                "email/agent_tools.json", "push"):
        rm(pre + rel)
# 4. Config: MCP servers (+ sealed headers), the /v1 endpoint token, backups that still hold provider keys.
for rel in ("config/mcp_secrets", "config/mcp_servers.json", "config/core_endpoint.json"):
    rm(rel)
cfg = dest / "config"
if cfg.is_dir():
    for p in sorted(cfg.iterdir()):
        if ".bak" in p.name or ".migrated" in p.name:
            rm(str(p.relative_to(dest)))
# 5. Process records of the live gateway (pid files the app reaper would act on), stamp/policy secrets, dev tokens.
for rel in ("run", "gateway_paused.json", ".workflow_policy_secret", "entities/.stamp_secret", "af_stack.pid",
            "observer.pid", "dev"):
    rm(rel)
# 6. Account records: keep the accounts (their planes exist), drop their token hashes and named keys.
users = dest / "auth" / "users.json"
if users.is_file():
    inside(users)
    doc = json.loads(users.read_text(encoding="utf-8"))
    n_keys = 0
    for u in doc.get("users") or []:
        n_keys += len(u.get("openai_keys") or [])
        u["openai_keys"] = []
        u.pop("token_hash", None)
        u.pop("token_fingerprint", None)
    users.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    edited.append(f"auth/users.json: {len(doc.get('users') or [])} accounts kept, token_hash/token_fingerprint "
                  f"removed, {n_keys} named API key record(s) removed")
# 7. Runtime settings: loopback only, backlog exec runner off, no tray.
rc = dest / "config" / "runtime_config.json"
if rc.is_file():
    inside(rc)
    doc = json.loads(rc.read_text(encoding="utf-8"))
    doc["network"] = {"exposure": "localhost", "port": 18080}
    doc["backlog_exec_runner"] = False
    doc["desktop_tray"] = False
    rc.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    edited.append("config/runtime_config.json: network=localhost:18080, backlog_exec_runner=false, desktop_tray=false")
# 8. Pause execution: the runner keeps its loop (and its store probes) but schedules no tick.
pause = dest / "gateway_paused.json"
inside(pause.parent)
pause.write_text(json.dumps({"paused": True, "paused_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "paused_by": "perf-fixture", "reason": "benchmark fixture: no run may execute"}) + "\n",
                 encoding="utf-8")
edited.append("gateway_paused.json: written (paused): the runner schedules no tick on this clone")

# Anything secret-shaped left anywhere (outside run/ledger files and workspaces)? Fail loudly.
left = []
for p in dest.rglob("*"):
    n = p.name
    if n.startswith(("run_", "ledger_")) or "/workspaces/" in str(p) or "/artifacts/" in str(p):
        continue
    if n in ("sealing.key", "secret.enc", "devices.json", "token_owners.json", "sessions.json",
             "bootstrap-admin-token", "recovery_codes.json", "session_secret") or n.endswith(".enc"):
        if not p.name == "sessions.json" or p.parent.name == "auth":
            left.append(str(p.relative_to(dest)))
if left:
    raise SystemExit("SCRUB INCOMPLETE, still present: " + ", ".join(left[:20]))

readme = dest / "README-scrub.md"
inside(readme.parent)
lines = [
    "# Benchmark fixture (scrubbed clone)",
    "",
    f"Cloned {time.strftime('%Y-%m-%d %H:%M:%S %Z')} from `{src}` with `cp -c -R` (APFS clone) by",
    "`abstractgateway/scripts/perf/clone_runtime.sh`. Not the operator's data: never copy anything back.",
    "",
    "Left out of the clone: `apps/` (installed browser apps; their pid files and ports belong to the live gateway),",
    "`dev/` (a Chrome profile and dev tokens).",
    "",
    f"## Removed ({len(removed)})",
    "",
    *[f"- `{r}`" for r in removed],
    "",
    "## Edited",
    "",
    *[f"- {e}" for e in edited],
    "",
    "Not in this folder at all: provider API keys (environment, `~/.abstractcore`). The benchmark gateway runs with",
    "an empty environment and a scratch HOME, so it cannot read them.",
    "",
]
readme.write_text("\n".join(lines), encoding="utf-8")
print(f"scrubbed: {len(removed)} paths removed, {len(edited)} edits; see {readme}")
(dest.parent / ".clone-scrubbed.json").write_text(json.dumps({"removed": removed}), encoding="utf-8")
PYEOF

# Proof the source is untouched.
sums > "$SUMS_AFTER"
if ! diff -q "$SUMS_BEFORE" "$SUMS_AFTER" >/dev/null; then
  echo "FAIL: a source file changed during the clone:" >&2; diff "$SUMS_BEFORE" "$SUMS_AFTER" >&2; exit 1
fi
echo "source checksums unchanged ($(grep -vc '^absent' "$SUMS_AFTER") files hashed before and after)"
NEWER="$STAMP_DIR/.clone-source-newer"
(cd "$SRC" && find . -newer "$MARKER" -not -type d) | sed 's|^\./||' | sort > "$NEWER"
"$PY" -I - "$NEWER" "$STAMP_DIR/.clone-scrubbed.json" "$SRC" <<'PYEOF'
import json, os, sys
newer = [l.strip() for l in open(sys.argv[1]) if l.strip()]
removed = json.load(open(sys.argv[2]))["removed"]
src = sys.argv[3]
# Every path the scrub removed from the clone must still exist in the source (a scrub that hit the source would
# have deleted it). Content of the files the live gateway never rewrites is compared by hash above.
gone = [r for r in removed if not os.path.lexists(os.path.join(src, r))]
if gone:
    raise SystemExit("FAIL: paths scrubbed in the clone are missing from the source: " + ", ".join(gone))
print(f"all {len(removed)} scrubbed paths still present in the source")
print(f"source files modified since the marker: {len(newer)}")
for n in newer[:12]:
    print(f"  {n}")
if len(newer) > 12:
    print(f"  ... {len(newer) - 12} more in {sys.argv[1]}")
if newer:
    print("  (written by the gateway running on the source: heartbeats, watcher state, sessions, logs, incidents;")
    print("   this script only reads the source: every write goes through a guard that refuses paths outside the clone)")
PYEOF
du -sh "$DEST" 2>/dev/null | awk '{print "clone apparent size: " $1 " (shared blocks, APFS clone)"}'
