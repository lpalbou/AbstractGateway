# AbstractGateway — Getting started

AbstractGateway is a deployable HTTP/SSE host for **durable AbstractRuntime runs**:
- clients **start runs** and submit **durable commands**
- clients **render** by replaying/streaming the durable ledger (replay-first)

This guide gets a new installation running in **bundle mode** (recommended), then covers **file vs SQLite** durability and a best-effort **file → SQLite** migration.

## AbstractFramework ecosystem (context)

AbstractGateway is one component in the larger **AbstractFramework** ecosystem:
- **AbstractRuntime** (required): durable runs + workflow registry + stores
- **AbstractCore** (optional, via `abstractruntime[abstractcore]`): LLM/tool execution wiring used by many bundles

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

## Prerequisites

- Python `>=3.10` (see `pyproject.toml`)
- Workflow source:
  - **Bundle mode** (recommended): one `.flow` file or a directory of `*.flow` bundles
    - You can also upload bundles after startup via `POST /api/gateway/bundles/upload` (see below)
  - **VisualFlow directory mode** (compat): a directory of `*.json` VisualFlow files (requires `abstractgateway[visualflow]`)

## Install

```bash
# Core package (runner + stores + CLI)
pip install abstractgateway

# HTTP server (FastAPI + Uvicorn)
pip install "abstractgateway[http]"

# Optional: voice/audio (TTS + STT endpoints)
pip install "abstractgateway[voice]"

# Or: batteries-included (HTTP + tools + voice + media + visualflow)
pip install "abstractgateway[all]"
```

Optional (only if your workflows need it):
- LLM/tool nodes (bundle mode): `pip install "abstractruntime[abstractcore]>=0.4.2"` (already included by `abstractgateway[http]`)
- Visual Agent nodes (bundle mode): `pip install abstractagent` (already included by `abstractgateway[http]`)
- Voice/audio endpoints (TTS/STT): `pip install "abstractgateway[voice]"` (or `pip install abstractvoice`)
- `memory_kg_*` nodes (bundle mode): `pip install "abstractmemory[lancedb]"` (or `abstractmemory` + `lancedb`)

## 1) Run (bundle mode, file-backed stores)

File-backed stores are the default and easiest for dev.

```bash
export ABSTRACTGATEWAY_WORKFLOW_SOURCE=bundle
export ABSTRACTGATEWAY_FLOWS_DIR="/path/to/bundles"      # directory with *.flow (or a single .flow file)
export ABSTRACTGATEWAY_DATA_DIR="$PWD/runtime/gateway"

# Required by default: the server refuses to start without a token.
export ABSTRACTGATEWAY_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="http://localhost:*,http://127.0.0.1:*"

abstractgateway serve --host 127.0.0.1 --port 8080
```

OpenAPI docs (Swagger UI): `http://127.0.0.1:8080/docs`

Smoke checks:

```bash
curl -sS "http://127.0.0.1:8080/api/health"

curl -sS -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  "http://127.0.0.1:8080/api/gateway/bundles"
```

If `bundles.items` is empty, either:
- point `ABSTRACTGATEWAY_FLOWS_DIR` at a directory containing `*.flow` files (or a single `.flow` file), or
- upload a bundle via the API:

```bash
curl -sS -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  -F "file=@./my-bundle@0.1.0.flow" \
  -F "overwrite=false" \
  -F "reload=true" \
  "http://127.0.0.1:8080/api/gateway/bundles/upload"
```

## 2) Start a run (bundle mode)

First, discover entrypoints from `GET /api/gateway/bundles`. Then start a run:

```bash
curl -sS -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"bundle_id":"my-bundle","input_data":{"prompt":"Hello"}}' \
  "http://127.0.0.1:8080/api/gateway/runs/start"
```

Notes:
- If a bundle has multiple entrypoints and no default, you must pass `flow_id`.
- See [api.md](./api.md) for ledger replay/stream and durable commands.

## 2b) (Optional) Schedule a run (bundle mode)

To launch a workflow periodically, start a **scheduled parent run**:

```bash
curl -sS -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"bundle_id":"my-bundle","flow_id":"ac-echo","input_data":{"prompt":"Ping"},"start_at":"now","interval":"1h","repeat_count":3}' \
  "http://127.0.0.1:8080/api/gateway/runs/schedule"
```

Tip: to stop a schedule, cancel the scheduled parent run (`POST /api/gateway/commands`, type `cancel`).

## 3) Split API vs runner (recommended for upgrades)

By default, `abstractgateway serve` starts the HTTP API **and** the runner loop in the same process.

To restart the HTTP API without pausing durable execution, run two processes sharing the same `ABSTRACTGATEWAY_DATA_DIR`:

```bash
# Process 1 (runner worker, no HTTP deps needed):
abstractgateway runner

# Process 2 (HTTP API only):
abstractgateway serve --no-runner --host 127.0.0.1 --port 8080
```

## 4) What’s stored in `ABSTRACTGATEWAY_DATA_DIR` (file backend)

When `ABSTRACTGATEWAY_STORE_BACKEND=file` (default), the gateway persists (via `abstractruntime` stores):
- `run_<run_id>.json` (checkpointed run state)
- `ledger_<run_id>.jsonl` (append-only step records)
- `commands.jsonl` and `commands_cursor.json` (durable inbox + runner cursor)
- `artifacts/` (offloaded blobs/attachments)
- `dynamic_flows/` (gateway-generated wrapper flows, e.g. schedules)
- `workspaces/` (per-run workspaces created at run start when `workspace_root` is not provided)

## 5) Enable SQLite-backed stores

SQLite-backed stores eliminate directory scanning and move run/ledger/inbox data into indexed tables.

Artifacts remain file-backed under `ABSTRACTGATEWAY_DATA_DIR/artifacts/`.

```bash
export ABSTRACTGATEWAY_STORE_BACKEND=sqlite

# Optional; when omitted, defaults to: <ABSTRACTGATEWAY_DATA_DIR>/gateway.sqlite3
export ABSTRACTGATEWAY_DB_PATH="$PWD/runtime/gateway/gateway.sqlite3"
#
# Safety invariant: when using sqlite, the DB file must live under ABSTRACTGATEWAY_DATA_DIR.
# The gateway will refuse to start if ABSTRACTGATEWAY_DB_PATH points outside (prevents UAT/prod cross-wiring).

abstractgateway serve --host 127.0.0.1 --port 8080
```

## 6) Migrate an existing file-backed data dir → SQLite

This is a **best-effort** local migration (`abstractgateway migrate`) that reads:
- `run_*.json`
- `ledger_*.jsonl`
- `commands.jsonl`
- `commands_cursor.json`

and writes a single SQLite DB file. It does **not** delete the original files.

```bash
cp -a runtime/gateway "runtime/gateway.file-backup.$(date +%Y%m%d-%H%M%S)"

abstractgateway migrate --from=file --to=sqlite \
  --data-dir runtime/gateway \
  --db-path runtime/gateway/gateway.sqlite3
```

## Related docs

- Docs index: [README.md](./README.md)
- FAQ: [faq.md](./faq.md)
- Architecture: [architecture.md](./architecture.md)
- Configuration (env vars + optional deps): [configuration.md](./configuration.md)
- API overview: [api.md](./api.md)
- Security: [security.md](./security.md)
- Operator tooling (optional): [maintenance.md](./maintenance.md)
