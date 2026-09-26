# AbstractGateway — FAQ

This FAQ answers recurring questions from people integrating or operating
`abstractgateway`. For symptom-by-symptom fixes, see
[troubleshooting.md](./troubleshooting.md). For the full API surface, use the
live OpenAPI spec (`/openapi.json`, `/docs`), which is generated from the code.

## Getting started

### What is AbstractGateway?

AbstractGateway is a **durable run gateway** for AbstractRuntime:
- starts runs from `.flow` workflow bundles
- accepts a **durable command inbox** (commands are appended, then applied asynchronously by the runner)
- exposes a **replay-first ledger** API (SSE is optional)
- is the control plane of an AbstractFramework installation: users, providers,
  capability defaults, local engines, models, browser apps and network
  exposure, from the web console, the terminal console, the CLI or the tray

Evidence: `src/abstractgateway/routes/gateway.py`, `src/abstractgateway/runner.py`, `src/abstractgateway/service.py`.

### How does this fit in the AbstractFramework ecosystem?

- **AbstractRuntime** (required): the durable run model + tick loop + stores (declared in `pyproject.toml`).
- **AbstractGateway** (this repo): a deployable HTTP/SSE facade around AbstractRuntime runs (API in `src/abstractgateway/routes/gateway.py`).
- **AbstractCore, AbstractAgent, AbstractMemory** (installed with the gateway): Runtime owns the LLM/tool/media integration boundary; Gateway uses its discovery and run facades for prompt-cache controls, generated and edited media, voice, audio and music, and KG-backed bundle execution (`src/abstractgateway/hosts/bundle_host.py`).
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

### Can the gateway run VisualFlow JSON files directly?

No. Bundle mode is the only workflow source:

- input: one `.flow` file or a directory of `*.flow` bundles
  (`ABSTRACTGATEWAY_FLOWS_DIR`; the shipped bundles when unset);
- versioning: bundles are addressed as `bundle_id@bundle_version`.

Store VisualFlows through `/api/gateway/visualflows/*` and publish them as a
`.flow` bundle with `POST /api/gateway/visualflows/{flow_id}/publish`.

Evidence: `src/abstractgateway/service.py`, `src/abstractgateway/hosts/bundle_host.py`.

## Security

### Do I need to configure authentication?

Not on your own computer. A plain `abstractgateway serve` with nothing
configured binds `127.0.0.1`, turns user accounts on, creates the admin
account and prints a one-time sign-in link ([first-run.md](./first-run.md)).

To let other devices reach the gateway, choose the **network setting**
(`abstractgateway network set lan`, the console's **Network** tab or the
tray); user accounts stay on. A `--host 0.0.0.0` without any auth posture is
refused ([troubleshooting.md](./troubleshooting.md#serve-says-refusing-to-start-no-sign-in-would-protect-this-gateway)).

`ABSTRACTGATEWAY_AUTH_TOKEN` is a shared server/operator token that maps to
`local-admin`; it is not a browser sign-in token. Use user accounts for the
console and the browser apps.

Evidence: `src/abstractgateway/cli.py` (`serve`), `src/abstractgateway/first_run.py`.

### What is the difference between the bind address and the allowed origins?

- The bind address decides which network interfaces the server listens on.
  Set it with the network setting (`localhost`, `lan`, `internet`);
  `serve --host` overrides it for one run.
- The origin allowlist decides which browser pages may call `/api/gateway/*`
  (requests that carry an `Origin` header). Add origins with
  `abstractgateway network set --allowed-origins …`;
  `ABSTRACTGATEWAY_ALLOWED_ORIGINS` in the launch environment pins it.

Evidence: CLI flags in `src/abstractgateway/cli.py`, origin checks in `src/abstractgateway/security/gateway_security.py`.

### Why do I get `401` / `403` / `429` / `413` from `/api/gateway/*`?

See the status table in
[troubleshooting.md](./troubleshooting.md#401-403-429-or-413-from-apigateway).

### Can I disable security (dev only)?

Prefer keeping security enabled, even in dev.

If you must relax it:
- disable the gateway security layer entirely: `ABSTRACTGATEWAY_SECURITY=0`
- or (safer) allow unauthenticated reads on loopback only: `ABSTRACTGATEWAY_DEV_READ_NO_AUTH=1`
- or fine-tune: `ABSTRACTGATEWAY_PROTECT_READ=0`, `ABSTRACTGATEWAY_PROTECT_WRITE=0`

Evidence: env policy loader in `src/abstractgateway/security/gateway_security.py`.

### Why does the workspace browser never show `~/.ssh` or the gateway's data folder?

The gateway's data folder and the credential folders of its user account are
on a built-in deny list: never listed or served, and denied to runs' file
tools by default, even when a run's folder contains them. See
[security.md](./security.md#built-in-deny-list).

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

### Which workflow does "Gateway default" run?

The one saved for the app's agent interface under
`agents.default_workflow.<interface>` (for example
`abstractcode.agent.v1`); with nothing saved, AbstractCode's interface runs the
shipped `basic-agent`, and the Assistant's interface has no gateway default
(the Assistant uses its built-in orchestrator). Apps start such runs with
`flow_id: "@default"` and the interface; the gateway resolves it at every
start, answers the choice as `resolved_workflow`, and refuses the start (409)
rather than run another workflow when the saved one cannot run. Change it in
the console (Workflows), the terminal console, or with
`abstractgateway config set agents.default_workflow.<interface> bundle[@version]:flow`.
See [configuration.md](./configuration.md#default-agent-workflow).

### Can I see the reply while the model writes it?

Yes. Start the run with `input_data._runtime.stream: true` (or turn on
`agents.streaming_default` so interactive runs stream when the app does not
say) and read the run's ledger stream: the text arrives as `llm.delta`
frames, and the durable `llm_call` record still carries the final answer.
See [api.md](./api.md#4b-live-replies-token-deltas-on-the-same-stream).

### Where does a bundle's default model come from?

From the execution-host `input.text` capability route (the console's
**Multimodal** tab, **Use as default** in **Models**, or
`abstractgateway-config set-default input.text …`). A flow can also pin a
provider and model on its `llm_call` or `agent` nodes. When nothing is
configured, the run fails with a clear configuration error; see
[troubleshooting.md](./troubleshooting.md#llm-nodes-but-no-default-providermodel-is-configured).

Evidence: `src/abstractgateway/provider_defaults.py`, `src/abstractgateway/hosts/bundle_host.py`.

### Why do tool calls not execute?

In bundle mode, tool execution is controlled by:

- `ABSTRACTGATEWAY_TOOL_MODE=approval` (default): safe tools execute immediately; dangerous/unknown tools pause for explicit approval.
- `ABSTRACTGATEWAY_TOOL_MODE=passthrough`: approval required for *all* tools (including safe ones); after approval, the runtime executes the tool batch in-process.
- `ABSTRACTGATEWAY_TOOL_MODE=delegated`: tools are not executed locally; workflows enter a durable `JOB` wait for external executors.
- `ABSTRACTGATEWAY_TOOL_MODE=local` (or `local_all`): tools execute inside the gateway process without approval (dev only; unsafe).

Evidence: tool executor selection in `src/abstractgateway/hosts/bundle_host.py`.

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

### Are catalog responses normalized by Gateway?

Catalog routes carry a Gateway-owned envelope: `catalog` (contract
`gateway_catalog_v1`, version, kind, scope and source) plus a canonical
`items` array. Read those. The lower-layer fields (`models`, `providers`,
`provider_models`, `profiles`, `voices`) stay in the payload for
compatibility, and their shapes differ from route to route.

The capabilities contract also carries `common.readiness`, a compact summary
of Gateway-owned surface readiness. Deeper backend and provider diagnostics
belong to Runtime and AbstractCore, and the gateway reports them only when
those layers expose them. See [api.md](./api.md#discovery-endpoints-optional).

### What does Gateway session prompt-cache orchestration include?

The `/api/gateway/prompt_cache/*` routes expose provider/model prompt-cache
controls when the active AbstractCore integration supports them. Gateway also
provides session lifecycle routes under
`/api/gateway/sessions/{session_id}/prompt_cache/*` for status, prepare,
rebuild, and clear using deterministic session keys.

This is Gateway-owned naming and orchestration over provider controls, not a
provider-independent local KV cache or full CachedSession persistence system.

### Which KG memory backend should I use?

Keep the default `lancedb` backend for durable, vector-capable memory; use
`memory` for process-local dev/test memory; set
`ABSTRACTGATEWAY_MEMORY_STORE_BACKEND=sqlite` only when your installed
AbstractMemory exposes `SQLiteTripleStore`. A fresh persistent store is
reported as available: structured queries return no matches until a flow
asserts triples.

Evidence: `src/abstractgateway/memory_store.py`.

## Desktop tray

### How do I get the menu bar / tray icon, and why is there none?

Install the extra (`pip install "abstractgateway[tray]"`) and start the
gateway with `abstractgateway serve` on a desktop session. The icon has no
on/off setting: while the gateway serves a desktop that can show it, it is
there. The boot log says `Desktop tray: started (pid …)` or names the reason
it is absent (`missing_dependency`, `headless`, `dev_reload`, `runner_only`,
`no_tray_flag` for `serve --no-tray`);
the console's **Resources** tab shows the same. On Linux the GTK/AppIndicator
bindings are needed; GNOME also needs the AppIndicator extension. Details:
[tray.md](./tray.md) and
[troubleshooting.md](./troubleshooting.md#there-is-no-tray-icon).

### What does "Pause Workflows" actually stop?

New workflow steps. Runs, schedules and messages from connected apps are
still accepted and wait; a step already inside an LLM or tool call finishes
first; the console and the API keep answering; summoned entities' own loops
are separate processes and keep their schedule. Pause persists across
restarts until you resume (tray, console banner, or
`POST /api/gateway/host/resume`).

## Deployment

### How do I run API and runner as separate processes?

Run:

```bash
abstractgateway runner
abstractgateway serve --no-runner --host 127.0.0.1 --port 8080
```

The runner uses a lock file (`gateway_runner.lock`) to prevent double-ticking on the same data dir. A locked-out runner keeps retrying acquisition in the background; a newly started process asks a live holder to yield (newest process wins), and the holder heartbeats the lock file so `GET /api/health` can report whether anyone is actually ticking the data dir (`runner.runners[].status`).

Evidence: CLI flag `--no-runner` in `src/abstractgateway/cli.py`, lock lifecycle (`_run`/`_acquire_singleton_lock`/`runner_status`) in `src/abstractgateway/runner.py`.

## Related docs

- Docs index: [README.md](./README.md)
- Troubleshooting: [troubleshooting.md](./troubleshooting.md)
- Getting started: [getting-started.md](./getting-started.md)
- API overview: [api.md](./api.md)
- Security: [security.md](./security.md)
- Configuration: [configuration.md](./configuration.md)
- Architecture: [architecture.md](./architecture.md)
- Operator tooling (optional): [maintenance.md](./maintenance.md)
