# AbstractGateway — FAQ

This FAQ is written for first-time users integrating or operating `abstractgateway`.
For the full API surface, rely on the live OpenAPI spec (`/openapi.json`, `/docs`) which is generated from code.

## Getting started

### What is AbstractGateway?

AbstractGateway is a **durable run gateway** for AbstractRuntime:
- starts runs from workflows (bundle mode or visualflow directory mode)
- accepts a **durable command inbox** (commands are appended, then applied asynchronously by the runner)
- exposes a **replay-first ledger** API (SSE is optional)

Evidence: `src/abstractgateway/routes/gateway.py`, `src/abstractgateway/runner.py`, `src/abstractgateway/service.py`.

### Do I need AbstractFlow to run workflows?

Not for **bundle mode** (the default).

- Bundle mode loads `.flow` bundles and compiles VisualFlow JSON via `abstractruntime.visualflow_compiler` (no `abstractflow` import).
- You only need `abstractflow` to **author** bundles, or to run **visualflow directory mode**.

Evidence: `src/abstractgateway/hosts/bundle_host.py` (bundle compilation), `src/abstractgateway/hosts/visualflow_host.py` (requires `abstractflow`).

### What’s the difference between bundle mode and visualflow directory mode?

- **Bundle mode** (`ABSTRACTGATEWAY_WORKFLOW_SOURCE=bundle`, default):
  - input: one `.flow` file or a directory of `*.flow`
  - versioning: bundles are addressed as `bundle_id@bundle_version`
- **VisualFlow directory mode** (`ABSTRACTGATEWAY_WORKFLOW_SOURCE=visualflow`):
  - input: a directory of `*.json` VisualFlow files
  - requires: `pip install "abstractgateway[visualflow]"`

Evidence: `src/abstractgateway/service.py` (workflow source switch), `src/abstractgateway/hosts/bundle_host.py`, `src/abstractgateway/hosts/visualflow_host.py`.

## Security

### Why does `abstractgateway serve` refuse to start?

By default, the server requires an auth token for `/api/gateway/*` and will fail-fast if none is configured.

Fix:

```bash
export ABSTRACTGATEWAY_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

Evidence: startup self-check in `src/abstractgateway/cli.py`, policy loading in `src/abstractgateway/security/gateway_security.py`.

### What’s the difference between `--host` and `ABSTRACTGATEWAY_ALLOWED_ORIGINS`?

- `abstractgateway serve --host ...` controls the **bind address** (network interfaces the server listens on).
- `ABSTRACTGATEWAY_ALLOWED_ORIGINS` controls an **Origin allowlist** for requests that include an `Origin` header (browser/origin defense) on `/api/gateway/*`.

Evidence: CLI flags in `src/abstractgateway/cli.py`, origin checks in `src/abstractgateway/security/gateway_security.py`.

### Why do I get `401` / `403` / `429` / `413` from `/api/gateway/*`?

Common causes:
- `401 Unauthorized`: missing/invalid `Authorization: Bearer <token>`
- `403 Forbidden (origin not allowed)`: browser `Origin` not matched by `ABSTRACTGATEWAY_ALLOWED_ORIGINS`
- `429 Too Many Requests (auth lockout)`: repeated auth failures from the same client IP (lockout backoff)
- `413 Payload Too Large`: request exceeds configured body/upload limits

Evidence: `GatewaySecurityMiddleware.__call__` in `src/abstractgateway/security/gateway_security.py`.

### Can I disable security (dev only)?

Prefer keeping security enabled, even in dev.

If you must relax it:
- disable the gateway security layer entirely: `ABSTRACTGATEWAY_SECURITY=0`
- or (safer) allow unauthenticated reads on loopback only: `ABSTRACTGATEWAY_DEV_READ_NO_AUTH=1`
- or fine-tune: `ABSTRACTGATEWAY_PROTECT_READ=0`, `ABSTRACTGATEWAY_PROTECT_WRITE=0`

Evidence: env policy loader in `src/abstractgateway/security/gateway_security.py`.

## Storage

### Where is data stored?

Everything is rooted at `ABSTRACTGATEWAY_DATA_DIR`:

- File backend (default): `run_*.json`, `ledger_*.jsonl`, `commands.jsonl`, `commands_cursor.json`, plus `artifacts/`
- SQLite backend: a single DB file (default `<DATA_DIR>/gateway.sqlite3`) plus `artifacts/`
- Gateway-generated workflows (e.g. schedules): `dynamic_flows/`
- Per-run workspaces (when `workspace_root` is not provided at start): `workspaces/`

Evidence: `src/abstractgateway/stores.py`, `src/abstractgateway/routes/gateway.py` (`start_run` workspace default), `src/abstractgateway/hosts/bundle_host.py` (dynamic flows).

### How do I switch to SQLite? Can I migrate?

- Switch by setting `ABSTRACTGATEWAY_STORE_BACKEND=sqlite` (and optionally `ABSTRACTGATEWAY_DB_PATH`).
- Migrate file → SQLite with `abstractgateway migrate --from=file --to=sqlite ...` (best-effort local migration).

Evidence: `src/abstractgateway/stores.py`, `src/abstractgateway/migrate.py`, CLI wiring in `src/abstractgateway/cli.py`.

## Runs, ledger, commands

### What is the ledger, and what does `after` mean?

- The ledger is an **append-only** list of step records.
- `after` is a cursor meaning “number of records already consumed”; responses return `next_after`.
- SSE streams ledger updates, but clients should always reconnect by replaying from the last cursor.

Evidence: `GET /runs/{run_id}/ledger` and `/ledger/stream` in `src/abstractgateway/routes/gateway.py`.

### How do durable commands work? When do they take effect?

`POST /api/gateway/commands` appends a command record to a durable inbox.
The background runner polls the inbox and applies commands asynchronously.

Supported command types:
`pause|resume|cancel|emit_event|update_schedule|compact_memory`

Evidence: `submit_command` in `src/abstractgateway/routes/gateway.py`, command application in `src/abstractgateway/runner.py`.

## Bundles and workflow execution

### How do I run a specific bundle version?

When starting runs in bundle mode you can select versions in two ways:
- pass `bundle_id` + `bundle_version`
- or pass a namespaced `flow_id` like `bundle@version:flow` (this also works for selecting “latest” via `bundle:flow`)

Evidence: bundle selection in `src/abstractgateway/hosts/bundle_host.py` (`start_run`).

### My bundle fails with “LLM/tool execution requires AbstractCore integration”

Install AbstractRuntime’s AbstractCore integration (already included by `abstractgateway[http]`):

```bash
pip install "abstractruntime[abstractcore]>=0.4.0"
```

Evidence: `src/abstractgateway/hosts/bundle_host.py` (imports under `needs_llm/needs_tools`).

### My bundle fails with “LLM nodes but no default provider/model is configured”

Provide defaults via env vars:

```bash
export ABSTRACTGATEWAY_PROVIDER="lmstudio"
export ABSTRACTGATEWAY_MODEL="..."
```

The gateway may also infer defaults from flow JSON if provider/model are set on at least one `llm_call` or `agent` node.

Evidence: `_scan_flows_for_llm_defaults` + provider/model selection in `src/abstractgateway/hosts/bundle_host.py`.

### Why do tool calls not execute?

In bundle mode, tool execution is controlled by:

- `ABSTRACTGATEWAY_TOOL_MODE=passthrough` (default): tools do **not** execute in-process; workflows enter a durable “approval required” wait.
- `ABSTRACTGATEWAY_TOOL_MODE=local`: tools execute inside the gateway process (dev only; higher risk).

Evidence: tool executor selection in `src/abstractgateway/hosts/bundle_host.py`.

### My bundle fails with “Visual Agent nodes require AbstractAgent”

Install `abstractagent` (already included by `abstractgateway[http]`):

```bash
pip install abstractagent
```

Evidence: agent workflow registration in `src/abstractgateway/hosts/bundle_host.py`.

### My bundle fails with “memory_kg_* nodes … install abstractmemory”

`memory_kg_*` nodes require the AbstractMemory integration:

```bash
pip install "abstractmemory[lancedb]"
```

Evidence: memory KG wiring in `src/abstractgateway/hosts/bundle_host.py` (imports `abstractmemory` and `abstractruntime.integrations.abstractmemory`).

## Deployment

### How do I run API and runner as separate processes?

Run:

```bash
abstractgateway runner
abstractgateway serve --no-runner --host 127.0.0.1 --port 8080
```

The runner uses a lock file (`gateway_runner.lock`) to prevent double-ticking on the same data dir.

Evidence: CLI flag `--no-runner` in `src/abstractgateway/cli.py`, lock acquisition in `src/abstractgateway/runner.py`.

## Related docs

- Docs index: [README.md](./README.md)
- Getting started: [getting-started.md](./getting-started.md)
- API overview: [api.md](./api.md)
- Security: [security.md](./security.md)
- Configuration: [configuration.md](./configuration.md)
- Architecture: [architecture.md](./architecture.md)
- Operator tooling (optional): [maintenance.md](./maintenance.md)
