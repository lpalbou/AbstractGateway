# AbstractGateway — Getting Started

AbstractGateway is the deployable HTTP/SSE host for **durable AbstractRuntime runs**:
- clients submit durable commands (`start`, `resume`, `pause`, `cancel`, `emit_event`)
- clients render by replaying/streaming the append-only ledger

This document covers:
- running the gateway in **bundle mode** (recommended)
- choosing a durability backend (**file** vs **sqlite**)
- migrating an existing file-backed runtime directory to SQLite

## Prerequisites

- Python environment with:
  - `abstractgateway` (runner + durable gateway host), and
  - `abstractgateway[http]` if you want to run the HTTP/SSE server (`abstractgateway serve`).
- A bundles directory containing `*.flow` bundles (recommended).
- If you run workflows that use LLM nodes:
  - a provider reachable by AbstractCore (e.g. LMStudio).

## 1) Run (bundle mode, file-backed stores)

File-backed stores are the default. They are transparent and great for dev.

```bash
export ABSTRACTGATEWAY_WORKFLOW_SOURCE=bundle
export ABSTRACTGATEWAY_FLOWS_DIR="flows/bundles"
export ABSTRACTGATEWAY_DATA_DIR="runtime/gateway"

export ABSTRACTGATEWAY_AUTH_TOKEN="dev-token"
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="http://localhost:*,http://127.0.0.1:*"

# If your bundles contain LLM/Agent nodes:
export ABSTRACTGATEWAY_PROVIDER="lmstudio"
export ABSTRACTGATEWAY_MODEL="qwen/qwen3-next-80b"

# Tool execution:
# - passthrough (default): tool calls become a durable wait for external approval/execution
# - local: execute tools in-process (dev only; treat as high risk)
export ABSTRACTGATEWAY_TOOL_MODE="passthrough"

abstractgateway serve --host 127.0.0.1 --port 8081
```

### Split API vs runner (recommended for upgrades)

By default, `abstractgateway serve` starts the HTTP API **and** the runner loop in the same process.

To restart the HTTP API without pausing durable execution, run two processes sharing the same `ABSTRACTGATEWAY_DATA_DIR`:

```bash
# Process 1 (runner worker, no HTTP deps needed):
abstractgateway runner

# Process 2 (HTTP API only):
abstractgateway serve --no-runner --host 127.0.0.1 --port 8081
```

### `--host` vs `ABSTRACTGATEWAY_ALLOWED_ORIGINS` (common confusion)

- `abstractgateway serve --host ...` controls the **bind address** (network interfaces to listen on).
  - Local-only: `--host 127.0.0.1`
  - LAN/dev: `--host 0.0.0.0` (all interfaces) or `--host <your-lan-ip>` (e.g. `192.168.1.188`)
  - You cannot pass multiple hosts; if you need multiple binds, run behind a reverse proxy or run multiple gateway processes.
- `ABSTRACTGATEWAY_ALLOWED_ORIGINS` controls **CORS** (which browser Origins are allowed to call the API).
  - It can list multiple values like: `http://localhost:*,http://127.0.0.1:*,http://192.168.1.188:*`
  - It does **not** change which IPs the server listens on.
  - Non-browser clients (e.g. `curl`) are not subject to CORS, but still require auth if `ABSTRACTGATEWAY_AUTH_TOKEN` is set.

### What gets stored in `ABSTRACTGATEWAY_DATA_DIR` (file backend)

When `ABSTRACTGATEWAY_STORE_BACKEND=file` (default), the gateway persists:
- `run_<run_id>.json` (checkpointed run state)
- `ledger_<run_id>.jsonl` (append-only step records)
- `commands.jsonl` and `commands_cursor.json` (durable inbox + runner cursor)
- `artifacts/` (artifact blobs/refs; used for large payloads and attachments)
- `dynamic_flows/` (gateway-generated wrapper flows, e.g. schedules)

## 2) Enable SQLite-backed stores (recommended for large/long-running dirs)

SQLite-backed stores eliminate directory scanning and move run/ledger/inbox data into indexed tables.

Artifacts remain file-backed under `ABSTRACTGATEWAY_DATA_DIR/artifacts/`.

```bash
export ABSTRACTGATEWAY_STORE_BACKEND=sqlite

# Optional; when omitted, defaults to: <ABSTRACTGATEWAY_DATA_DIR>/gateway.sqlite3
export ABSTRACTGATEWAY_DB_PATH="$PWD/runtime/gateway/gateway.sqlite3"
```

Then start the gateway as usual:

```bash
abstractgateway serve --host 127.0.0.1 --port 8081
```

## 3) Migrate an existing file-backed data dir → SQLite

This is a **best-effort** local migration that *reads*:
- `run_*.json`
- `ledger_*.jsonl`
- `commands.jsonl`
- `commands_cursor.json`

and writes a single SQLite database file.

The migration does **not** delete your old `run_*.json` / `ledger_*.jsonl` files.

### Example: migrate `runtime/gateway/` into `runtime/gateway/gateway.sqlite3`

1) Stop the gateway (avoid concurrent writes).
2) Back up the directory (recommended).
3) Run the migration.

```bash
cp -a runtime/gateway "runtime/gateway.file-backup.$(date +%Y%m%d-%H%M%S)"

abstractgateway migrate --from=file --to=sqlite \
  --data-dir runtime/gateway \
  --db-path runtime/gateway/gateway.sqlite3
```

Now run in SQLite mode:

```bash
export ABSTRACTGATEWAY_STORE_BACKEND=sqlite
export ABSTRACTGATEWAY_DB_PATH="$PWD/runtime/gateway/gateway.sqlite3"
abstractgateway serve --host 127.0.0.1 --port 8081
```

### Smoke checks (SQLite mode)

List bundles:

```bash
curl -sS -H "Authorization: Bearer dev-token" \
  "http://127.0.0.1:8081/api/gateway/bundles"
```

List runs:

```bash
curl -sS -H "Authorization: Bearer dev-token" \
  "http://127.0.0.1:8081/api/gateway/runs?limit=5"
```

Verify the DB has data:

```bash
sqlite3 runtime/gateway/gateway.sqlite3 \
  "select count(*) as runs from runs; select count(*) as ledger_rows from ledger; select count(*) as commands from commands;"
```

## 4) LMStudio notes (Level C / real inference)

If you use `ABSTRACTGATEWAY_PROVIDER=lmstudio`, ensure LMStudio is running an OpenAI-compatible server (default: `http://localhost:1234/v1`).

Optional:

```bash
export LMSTUDIO_BASE_URL="http://localhost:1234/v1"
```

## 5) Tool execution modes (important)

- `ABSTRACTGATEWAY_TOOL_MODE=passthrough` (recommended default):
  - the workflow can request tools, but the gateway will pause in a durable wait (approval required).
- `ABSTRACTGATEWAY_TOOL_MODE=local` (dev only):
  - the gateway executes tools in-process via AbstractCore’s default tool map.
  - enabling comms tools (`ABSTRACT_ENABLE_COMMS_TOOLS=1`, email credentials, etc.) can have real-world side effects.

## Related docs

- Package architecture: `abstractgateway/docs/architecture.md`
- Framework architecture: `docs/architecture.md`
- Testing levels (A/B/C): `docs/adr/0019-testing-strategy-and-levels.md`
