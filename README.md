# AbstractGateway

AbstractGateway is the **deployable Run Gateway host** for AbstractRuntime runs:
- durable command inbox
- ledger replay/stream
- security baseline (token + origin + limits)

This decouples the gateway service from any specific UI (AbstractFlow, AbstractCode, web/PWA thin clients).

## What it does (contract)
- Clients **act** by submitting durable commands: `start`, `resume`, `pause`, `cancel`, `emit_event`
- Clients **render** by replaying/streaming the durable ledger (cursor-based, replay-first)

Endpoints:
- `POST /api/gateway/runs/start`
- `GET /api/gateway/runs/{run_id}`
- `GET /api/gateway/runs/{run_id}/ledger`
- `GET /api/gateway/runs/{run_id}/ledger/stream` (SSE)
- `POST /api/gateway/commands`

## Install

### Default (bundle mode)

```bash
pip install abstractgateway
```

Bundle mode executes **WorkflowBundles** (`.flow`) via **WorkflowArtifacts** without importing `abstractflow`.

### Optional (compatibility): VisualFlow JSON

```bash
pip install "abstractgateway[visualflow]"
```

This mode depends on the **AbstractFlow compiler library** (`abstractflow`) to interpret VisualFlow JSON (it does **not** require the AbstractFlow web UI/app).

## Run

```bash
export ABSTRACTGATEWAY_DATA_DIR="./runtime"
export ABSTRACTGATEWAY_FLOWS_DIR="/path/to/bundles-or-flow"

# Durable store backend (default: file)
# - file:   run_*.json + ledger_*.jsonl + commands.jsonl under ABSTRACTGATEWAY_DATA_DIR
# - sqlite: OLTP tables + wait_index in a single sqlite file (recommended for large/long-running dirs)
export ABSTRACTGATEWAY_STORE_BACKEND="file"  # or: sqlite
# When using sqlite, defaults to: <ABSTRACTGATEWAY_DATA_DIR>/gateway.sqlite3
export ABSTRACTGATEWAY_DB_PATH="$ABSTRACTGATEWAY_DATA_DIR/gateway.sqlite3"

# Security (recommended)
export ABSTRACTGATEWAY_AUTH_TOKEN="your-token"
# Allow your browser app Origin(s). Wildcard ports are supported (dev convenience).
# Example (LAN dev): "http://localhost:*,http://127.0.0.1:*,http://192.168.1.188:*"
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="http://localhost:*,http://127.0.0.1:*"

# LLM defaults (required if the bundle contains LLM/Agent nodes)
export ABSTRACTGATEWAY_PROVIDER="lmstudio"
export ABSTRACTGATEWAY_MODEL="qwen/qwen3-next-80b"
# Tools:
# - passthrough (default): tool calls become a durable approval wait (safe for remote/untrusted hosts)
# - local: execute tools inside the gateway process (dev only)
export ABSTRACTGATEWAY_TOOL_MODE="passthrough"  # or: local

abstractgateway serve --host 127.0.0.1 --port 8080
```

Notes:
- `--host` controls the *bind address* (which network interfaces the server listens on). It does **not** accept a list.
  - Local-only: `--host 127.0.0.1`
  - LAN: `--host 0.0.0.0` (all interfaces) or `--host <your-lan-ip>` (e.g. `192.168.1.188`)
  - If you bind to `0.0.0.0`, treat the gateway as reachable by other machines on your network; keep auth enabled.
- `ABSTRACTGATEWAY_WORKFLOW_SOURCE` defaults to `bundle`. Valid values:
  - `bundle` (default): `ABSTRACTGATEWAY_FLOWS_DIR` points to a directory containing `*.flow` bundles (or a single `.flow` file)
  - `visualflow` (compat): `ABSTRACTGATEWAY_FLOWS_DIR` points to a directory containing `*.json` VisualFlow files
- For production, run behind HTTPS (reverse proxy) and set exact allowed origins.
- `ABSTRACTGATEWAY_ALLOWED_ORIGINS` is **CORS** (browser policy). It can list multiple origins like:
  - `http://localhost:*,http://127.0.0.1:*,http://192.168.1.188:*`
  - This does **not** control which IPs the server listens on; it only controls which browser Origins can call the API.

## Creating a `.flow` bundle (authoring)

Use AbstractFlow to pack a bundle:

```bash
abstractflow bundle pack /path/to/root.json --out /path/to/bundles/my.flow --flows-dir /path/to/flows
```

## Starting a run (bundle mode)

If the bundle has a **single entrypoint** (or declares `manifest.default_entrypoint`), you can start it with just `bundle_id` + `input_data`:

```bash
curl -sS -X POST "http://localhost:8080/api/gateway/runs/start" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{"bundle_id":"my-bundle","input_data":{"prompt":"Hello"}}'
```

If the bundle exposes **multiple entrypoints** and has no default, you must select one with `flow_id`:

```bash
curl -sS -X POST "http://localhost:8080/api/gateway/runs/start" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{"bundle_id":"my-bundle","flow_id":"ac-echo","input_data":{"prompt":"Hello"}}'
```

For backwards-compatible clients, you can also pass a namespaced id as `flow_id` (`"my-bundle:ac-echo"`) without sending `bundle_id`.

## Switch to SQLite (recommended for production-like workloads)

Migration is best-effort and reads your existing file-backed data dir:
- `run_*.json` → `runs` table (+ `wait_index`)
- `ledger_*.jsonl` → `ledger` table (+ `ledger_heads`)
- `commands.jsonl` → `commands` table
- `commands_cursor.json` → `command_cursors`

Example:

```bash
# Stop the gateway first (avoid concurrent writes).
cp -a runtime/gateway "runtime/gateway.file-backup.$(date +%Y%m%d-%H%M%S)"

abstractgateway migrate --from=file --to=sqlite \
  --data-dir runtime/gateway \
  --db-path runtime/gateway/gateway.sqlite3

# Then run with:
export ABSTRACTGATEWAY_STORE_BACKEND=sqlite
export ABSTRACTGATEWAY_DB_PATH="$PWD/runtime/gateway/gateway.sqlite3"
abstractgateway serve --host 127.0.0.1 --port 8080
```

## Docs
- Getting started (env + sqlite migration): `abstractgateway/docs/getting-started.md`
- Architecture: `docs/architecture.md` (framework) and `abstractgateway/docs/architecture.md` (this package)
- Deployment: `docs/guide/deployment.md`
