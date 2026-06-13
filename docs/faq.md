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
- **AbstractRuntime + transitive capability packages** (required by the default server install): Runtime owns the LLM/tool/media integration boundary; Gateway uses its discovery/run facades for prompt-cache controls, generated and edited image plus voice/audio/music capabilities, and KG-backed bundle execution (`src/abstractgateway/hosts/bundle_host.py`).
- Higher-level UIs (optional): AbstractFlow (authoring/bundling), AbstractObserver / AbstractCode / thin clients (operations + rendering).

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

### Do I need AbstractFlow to run workflows?

Not for **bundle mode** (the default).

- Bundle mode loads `.flow` bundles and compiles VisualFlow JSON via `abstractruntime.visualflow_compiler` (no `abstractflow` import).
- You only need `abstractflow` to **author** bundles.

Evidence: `src/abstractgateway/hosts/bundle_host.py` (bundle compilation).

### What’s the difference between bundle mode and visualflow directory mode?

- **Bundle mode** (`ABSTRACTGATEWAY_WORKFLOW_SOURCE=bundle`, default):
  - input: one `.flow` file or a directory of `*.flow`
  - versioning: bundles are addressed as `bundle_id@bundle_version`
- **VisualFlow directory mode**: removed. Use VisualFlow CRUD + publish to `.flow` bundles, then run in bundle mode.

Evidence: `src/abstractgateway/service.py` (workflow source switch), `src/abstractgateway/hosts/bundle_host.py`.

## Security

### Why does `abstractgateway serve` refuse to start?

By default, the server requires either Gateway user auth or a legacy
server/operator bearer token for `/api/gateway/*` and will fail fast if neither
is configured.

Recommended browser-app fix:

```bash
export ABSTRACTGATEWAY_USER_AUTH=1
export ABSTRACTGATEWAY_DATA_DIR="$PWD/runtime/gateway"
abstractgateway serve --host 127.0.0.1 --port 8080

# Use user admin plus this token for first login.
cat "$ABSTRACTGATEWAY_DATA_DIR/auth/bootstrap-admin-token"
```

Use `ABSTRACTGATEWAY_AUTH_TOKEN` only for legacy server/operator bearer-token
deployments; it maps to `local-admin` and is not a browser sign-in token.

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

Configure the execution-host `input.text` route:

```bash
abstractgateway-config set-default input.text \
  --provider lmstudio \
  --model qwen/qwen3.6-35b-a3b \
  --base-url http://127.0.0.1:1234/v1
```

Alternatives:
- Pin provider/model on at least one `llm_call` or `agent` node; the gateway scans the flow JSON for defaults.
- Keep provider secrets in `abstractcore-config`; use `--base-url` on the capability route when the
  selected provider endpoint is not the provider default.

Evidence: `_scan_flows_for_llm_defaults` + provider/model selection in `src/abstractgateway/hosts/bundle_host.py`.

### Why do tool calls not execute?

In bundle mode, tool execution is controlled by:

- `ABSTRACTGATEWAY_TOOL_MODE=approval` (default): safe tools execute immediately; dangerous/unknown tools pause for explicit approval.
- `ABSTRACTGATEWAY_TOOL_MODE=passthrough`: approval required for *all* tools (including safe ones); after approval, the runtime executes the tool batch in-process.
- `ABSTRACTGATEWAY_TOOL_MODE=delegated`: tools are not executed locally; workflows enter a durable `JOB` wait for external executors.
- `ABSTRACTGATEWAY_TOOL_MODE=local` (or `local_all`): tools execute inside the gateway process without approval (dev only; unsafe).

Evidence: tool executor selection in `src/abstractgateway/hosts/bundle_host.py`.

### Why do `/voice/tts` or `/audio/transcribe` fail with “capability unavailable”?

Those endpoints are surfaced through Runtime's voice/audio integration path and
the required capability packages are included by the base `abstractgateway`
install. Verify the installed package set with:

```bash
pip show abstractgateway AbstractRuntime abstractcore
```

By default, the gateway allows the configured voice backend to download models on first use. If you disabled downloads (or want to enable them explicitly), set:

```bash
export ABSTRACTGATEWAY_VOICE_ALLOW_DOWNLOADS=1
```

For remote/OpenAI-compatible voice backends, configure the Gateway-scoped voice
environment variables in the gateway process, for example
`ABSTRACTGATEWAY_VOICE_TTS_ENGINE`, `ABSTRACTGATEWAY_VOICE_STT_ENGINE`,
`ABSTRACTGATEWAY_VOICE_REMOTE_BASE_URL`, and
`ABSTRACTGATEWAY_VOICE_REMOTE_API_KEY` (legacy `ABSTRACTVOICE_*` names still
work).

### How do I enable generated images, edited images, generated music, or other Runtime-managed multimodal outputs?

Use the base install for the Gateway control plane and remote/provider-backed
routes:

```bash
pip install abstractgateway
```

The base install includes Runtime-owned tool and multimodal integration and can
proxy to configured remote/provider routes. Remote embeddings are supported
through the `embedding.text` capability route when it points at OpenAI,
OpenRouter, Portkey, LM Studio, vLLM, another OpenAI-compatible endpoint, or a
remote AbstractCore server. Local sentence-transformer embeddings and
hardware-local image, audio, voice, and music engines are explicit opt-ins so a
light Linux install does not pull PyTorch/CUDA packages. Use
`abstractgateway[apple]` or `abstractgateway[gpu]` only when this Gateway host
should execute those local engines itself.

Generated images are available both inside Runtime workflows and through
Gateway's direct run-scoped endpoint:

```text
POST /api/gateway/runs/{run_id}/images/generate
POST /api/gateway/runs/{run_id}/images/edit
POST /api/gateway/runs/{run_id}/images/upscale
POST /api/gateway/runs/{run_id}/videos/generate
POST /api/gateway/runs/{run_id}/videos/from_image
```

The direct image and video endpoints use Runtime/Core output selectors and
store the result as a run artifact, so they still require a configured
Runtime-compatible vision/video backend. Image dimensions are optional
passthrough overrides; clients should not inject a default `512x512` request
because supported sizes depend on the selected provider/model. Image/video
routes also accept optional batch `count` / `n`, `seeds`, and ordered
`lora_adapters`; video routes additionally accept `flow_shift`, and batch
responses return `image_artifacts` / `video_artifacts` alongside the
compatibility singular artifact fields. Use
`GET /api/gateway/vision/adapters` when a thin client needs the compatible
installed adapter catalog for a selected provider/model/task. For long media
runs, stream the returned `child_run_id` ledger and watch `abstract.progress`
records. Image progress is best-effort and may only show start/complete when the
backend does not expose step progress.

Generated music is exposed through Gateway's direct Runtime child-run route and
its thin-client discovery/catalog contract:

```text
POST /api/gateway/runs/{run_id}/music/generate
GET /api/gateway/audio/music/providers
GET /api/gateway/audio/music/models
```

Higher apps should feature-detect music from
`capabilities.contracts.flow_editor.media.generated_music` or
`capabilities.contracts.assistant.media.generated_music`.

### What is `voice.listen` in the capabilities contract?

`voice.listen` is not a live server-side microphone transport. It is a
higher-app contract that tells clients how to handle local capture:

- capture audio on the client or host device
- either upload it to `POST /api/gateway/runs/{run_id}/audio/transcribe`
- or emit the configured event/command into the run contract

This keeps live capture UX owned by higher apps such as Assistant or Observer
while Gateway stays responsible for durable runs, artifacts, and transcription.

### Are catalog responses fully normalized by Gateway?

Not yet.

Gateway already owns the route family and the thin-client contract pointers for
provider/model discovery, but some catalog response bodies still preserve
lower-layer shape differences. Higher apps like Flow currently normalize a few
legacy variants when reading model/provider catalogs.

What is stable today:

- which discovery routes exist
- which contract fields point at those routes
- which media tasks and direct endpoints are available

What is not yet versioned as a strict Gateway contract:

- one canonical provider/model catalog response envelope across text, vision,
  voice, STT, and music
- a dedicated deployment/readiness block for operator dashboards

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

A fresh persistent store does not make KG memory unavailable. Capability
discovery treats the surface as available once AbstractMemory is installed and
the configured backend resolves; empty structured queries return empty results
until a flow asserts triples.

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
