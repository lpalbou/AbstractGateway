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

### How does this fit in the AbstractFramework ecosystem?

- **AbstractRuntime** (required): the durable run model + tick loop + stores (declared in `pyproject.toml`).
- **AbstractGateway** (this repo): a deployable HTTP/SSE facade around AbstractRuntime runs (API in `src/abstractgateway/routes/gateway.py`).
- **AbstractCore / AbstractVoice / AbstractVision / AbstractMemory** (required by the default server install): LLM/tool execution, provider-level prompt-cache controls, workflow-backed/direct generated image/voice/audio capabilities, and KG memory used by many bundles (`src/abstractgateway/hosts/bundle_host.py`).
- Higher-level UIs (optional): AbstractFlow (authoring/bundling), AbstractObserver / AbstractCode / thin clients (operations + rendering).

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

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
  - requires: the base `pip install abstractgateway`

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

### Can I schedule a workflow to run periodically?

Yes (bundle mode).

Use `POST /api/gateway/runs/schedule` to start a scheduled parent run that launches the target workflow as child runs over time.

Notes:
- `interval` supports compact durations like `15m`, `1h`, `2d`.
- If `interval` is set and `repeat_count` is omitted, the schedule repeats forever (until you cancel it).
- To stop the schedule, cancel the scheduled parent run via `POST /api/gateway/commands` with type `cancel`.

Evidence: `ScheduleRunRequest` + `start_scheduled_run` in `src/abstractgateway/routes/gateway.py`.

## Bundles and workflow execution

### How do I run a specific bundle version?

When starting runs in bundle mode you can select versions in two ways:
- pass `bundle_id` + `bundle_version`
- or pass a namespaced `flow_id` like `bundle@version:flow` (this also works for selecting “latest” via `bundle:flow`)

Evidence: bundle selection in `src/abstractgateway/hosts/bundle_host.py` (`start_run`).

### My bundle fails with “LLM/tool execution requires AbstractCore integration”

AbstractRuntime’s AbstractCore integration is included by the base
`abstractgateway` install. If this error appears, verify the installed package set with
`pip show AbstractRuntime abstractcore`.

Evidence: `src/abstractgateway/hosts/bundle_host.py` (imports under `needs_llm/needs_tools`).

### My bundle fails with “LLM nodes but no default provider/model is configured”

Provide defaults via env vars (most explicit):

```bash
export ABSTRACTGATEWAY_PROVIDER="lmstudio"
export ABSTRACTGATEWAY_MODEL="..."
```

Alternatives:
- Configure global defaults via AbstractCore config (`abstractcore --config`, inspect with `abstractcore --status`).
- Pin provider/model on at least one `llm_call` or `agent` node; the gateway scans the flow JSON for defaults.

Evidence: `_scan_flows_for_llm_defaults` + provider/model selection in `src/abstractgateway/hosts/bundle_host.py`.

### Why do tool calls not execute?

In bundle mode, tool execution is controlled by:

- `ABSTRACTGATEWAY_TOOL_MODE=approval` (default): safe tools execute immediately; dangerous/unknown tools pause for explicit approval.
- `ABSTRACTGATEWAY_TOOL_MODE=passthrough`: approval required for *all* tools (including safe ones); after approval, the runtime executes the tool batch in-process.
- `ABSTRACTGATEWAY_TOOL_MODE=delegated`: tools are not executed locally; workflows enter a durable `JOB` wait for external executors.
- `ABSTRACTGATEWAY_TOOL_MODE=local` (or `local_all`): tools execute inside the gateway process without approval (dev only; unsafe).

Evidence: tool executor selection in `src/abstractgateway/hosts/bundle_host.py`.

### Why do `/voice/tts` or `/audio/transcribe` fail with “capability unavailable”?

Those endpoints are backed by **AbstractVoice** (`abstractvoice`), which is
included by the base `abstractgateway` install. Verify the installed package
set with:

```bash
pip show abstractgateway abstractvoice abstractcore
```

By default, the gateway allows AbstractVoice to download models on first use. If you disabled downloads (or want to enable them explicitly), set:

```bash
export ABSTRACTGATEWAY_VOICE_ALLOW_DOWNLOADS=1
```

For remote/OpenAI-compatible voice backends, configure AbstractVoice's native
environment variables in the gateway process, for example
`ABSTRACTVOICE_TTS_ENGINE`, `ABSTRACTVOICE_STT_ENGINE`,
`ABSTRACTVOICE_REMOTE_BASE_URL`, and `ABSTRACTVOICE_REMOTE_API_KEY`.

### How do I enable generated images or Runtime-managed multimodal outputs?

Use the base install:

```bash
pip install abstractgateway
```

The base install includes `AbstractRuntime[multimodal]`, AbstractCore's
`vision`/`voice`/`audio` extras, `abstractvision>=0.3.4`, and
`abstractvoice>=0.9.3`. The server image defaults image and audio generation to
OpenAI remote endpoints; set `OPENAI_API_KEY` (or the `ABSTRACTGATEWAY_VISION_*` /
`ABSTRACTGATEWAY_VOICE_*` overrides) before expecting live generation to succeed. Use
`ABSTRACTGATEWAY_VISION_BACKEND=diffusers` or `sdcpp` only for custom images that
intentionally include those local engines.

Generated images are available both inside Runtime/Core workflows and through
Gateway's direct run-scoped endpoint:

```text
POST /api/gateway/runs/{run_id}/images/generate
```

The direct endpoint uses Runtime/Core image-generation selectors and stores the
result as a run artifact, so it still requires a configured
AbstractVision-compatible backend.

### What does Gateway session prompt-cache orchestration include?

The `/api/gateway/prompt_cache/*` routes expose provider/model prompt-cache
controls when the active AbstractCore integration supports them. Gateway also
provides session lifecycle routes under
`/api/gateway/sessions/{session_id}/prompt_cache/*` for status, prepare,
rebuild, and clear using deterministic session keys.

This is Gateway-owned naming and orchestration over provider controls, not a
provider-independent local KV cache or full CachedSession persistence system.

### My bundle fails with “Visual Agent nodes require AbstractAgent”

AbstractAgent is included by the base `abstractgateway` install. Verify the
installed package set with:

```bash
pip show abstractgateway abstractagent
```

Evidence: agent workflow registration in `src/abstractgateway/hosts/bundle_host.py`.

### My bundle fails with “memory_kg_* nodes … install abstractmemory”

`memory_kg_*` nodes use Gateway's AbstractMemory TripleStore integration,
included by the base `abstractgateway` install.

Keep the default `lancedb` backend for durable vector-capable memory, use
`memory` for process-local dev/test memory, or set
`ABSTRACTGATEWAY_MEMORY_STORE_BACKEND=sqlite` only when your installed
AbstractMemory build exposes `SQLiteTripleStore`.

Evidence: memory KG wiring in `src/abstractgateway/memory_store.py` and
`src/abstractgateway/hosts/bundle_host.py`.

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
