# AbstractGateway — Configuration

AbstractGateway is configured primarily via **environment variables** (plus a few CLI flags).

## Install extras (recommended)

The base install (`pip install abstractgateway`) is the remote-light server
profile: HTTP/SSE, durable stores, `AbstractRuntime[multimodal,mcp-worker]`,
Runtime-owned provider/media/tool support, AbstractAgent, AbstractFlow
compatibility (runs bundles produced by AbstractFlow; does not require the `abstractflow` package), and AbstractMemory/LanceDB KG support.

Optional extras (see `pyproject.toml`):
- `abstractgateway[apple]`: full native macOS Python profile with Apple-local engines and all non-NVIDIA framework capabilities; this is for native macOS, not Docker
- `abstractgateway[gpu]`: full native/container GPU profile with local GPU engines and all relevant framework capabilities; the NVIDIA Docker image uses this profile
- `abstractgateway[docs]`: MkDocs site tooling
- `abstractgateway[dev]`: local dev/test deps

Default dependency floors:
- `AbstractRuntime[multimodal,mcp-worker]>=0.4.25`
- `abstractagent>=0.3.9`
- `AbstractMemory[lancedb]>=0.2.6`

Gateway's KG resolver targets AbstractMemory's TripleStore API. It does not use
the newer memory-agent API directly.

## Configuration helper

Gateway has a first-class configuration helper:

```bash
abstractgateway-config status
abstractgateway-config init --env-file .env
abstractgateway config status --json
```

It reports Gateway auth/data/store/runtime defaults, Core-server handoff
configuration, memory-store selection, and package readiness. `init` writes a
private env file with a generated Gateway token. Provider API keys, provider
base URLs and Core server settings remain owned by `abstractcore-config`.

## Core environment variables

### Paths + workflow source

- `ABSTRACTGATEWAY_DATA_DIR`: durable data directory (default: `./runtime`)  
  Evidence: `src/abstractgateway/config.py` (`GatewayHostConfig.from_env`)
- `ABSTRACTGATEWAY_FLOWS_DIR`: workflows directory (default: `./flows`)  
  Evidence: `src/abstractgateway/config.py`
- `ABSTRACTGATEWAY_WORKFLOW_SOURCE`: `bundle` (default) or `visualflow`  
  Evidence: `src/abstractgateway/service.py` (`create_default_gateway_service`)

### Workspace policy (filesystem scope)

The gateway enforces a server-side workspace policy so thin clients cannot expand filesystem access by sending arbitrary paths.

Operator-controlled roots:
- `ABSTRACTGATEWAY_WORKSPACE_DIR`: base directory used for `/api/gateway/files/*` helpers and to clamp run-provided `workspace_root` / `workspace_allowed_paths`.
- `ABSTRACTGATEWAY_WORKSPACE_MOUNTS`: additional allowed roots, newline-separated `name=/abs/path`.

Client scope overrides (permissive; trusted machines only):
- `ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE=1` (or `ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE=1`) enables honoring client-provided `workspace_*` knobs, including `workspace_access_mode=all_except_ignored`.

Discoverability:
- `GET /api/gateway/workspace/policy` returns `{policy: {...}}` including whether client overrides are enabled (mount names only; no absolute paths).

Evidence: `src/abstractgateway/routes/gateway.py` (`_workspace_root`, `_workspace_mounts`, `_sanitize_run_workspace_policy`, `_client_workspace_scope_overrides_enabled`, `start_run`).

### Durability backend

- `ABSTRACTGATEWAY_STORE_BACKEND`: `file` (default) or `sqlite`  
  Evidence: `src/abstractgateway/service.py`
- `ABSTRACTGATEWAY_DB_PATH`: SQLite DB file path (optional; default: `<DATA_DIR>/gateway.sqlite3`)  
  Evidence: `src/abstractgateway/stores.py` (`build_sqlite_stores`)
  Note: for safety, when `ABSTRACTGATEWAY_STORE_BACKEND=sqlite`, the DB path must be **under** `ABSTRACTGATEWAY_DATA_DIR`.
  The gateway fails fast if `ABSTRACTGATEWAY_DB_PATH` points elsewhere (prevents cross-wiring UAT/prod durable state).

### KG memory store

Gateway selects an AbstractMemory TripleStore through a small resolver; it does
not implement memory stores itself.

- `ABSTRACTGATEWAY_MEMORY_STORE_BACKEND`: `lancedb` (default), `memory`, or `sqlite` when the installed AbstractMemory build exposes `SQLiteTripleStore`
- `ABSTRACTGATEWAY_MEMORY_STORE_PATH`: optional explicit store path
- `ABSTRACTGATEWAY_MEMORY_REQUIRE_VECTOR=1`: fail fast when the selected backend cannot satisfy semantic/vector recall

Backend behavior:

- `lancedb`: persistent and vector-capable; semantic `query_text` requires the execution-host
  `embedding.text` capability route.
- `sqlite`: persistent and structured-query only when `SQLiteTripleStore` is available; semantic `query_text` fails clearly.
- `memory`: process-local test/dev backend; non-durable.

The same resolver is used for bundle `memory_kg_*` nodes and
`POST /api/gateway/kg/query`. Capability discovery reports memory backend,
persistence, vector support, and embedder status. A missing on-disk store is not
an unavailable state by itself: when AbstractMemory is installed and the backend
resolves, fresh stores are authoring-ready and structured queries simply return
no matches until assertions are written.

### Runner tuning (advanced)

These map to `GatewayHostConfig` and `GatewayRunnerConfig`:
- `ABSTRACTGATEWAY_RUNNER`: `1` (default) / `0` to disable runner in-process  
  Evidence: `src/abstractgateway/config.py`, `src/abstractgateway/cli.py`
- `ABSTRACTGATEWAY_POLL_S` (default `0.25`)
- `ABSTRACTGATEWAY_COMMAND_BATCH_LIMIT` (default `200`)
- `ABSTRACTGATEWAY_TICK_MAX_STEPS` (default `100`)
- `ABSTRACTGATEWAY_TICK_WORKERS` (default `4`)
- `ABSTRACTGATEWAY_RUN_SCAN_LIMIT` (default `200`)

Evidence: `src/abstractgateway/config.py`, `src/abstractgateway/runner.py`.

## LLM/tool defaults (bundle mode)

Only needed when the loaded bundle(s) contain LLM/tool/agent nodes.

- `output.text` capability route
  Default text route for LLM execution and Gateway LLM helper endpoints. Configure it through
  `abstractgateway-config set-default output.text ...` or `abstractcore --set-global-default ...`.
  If no pair is configured, helpers return a clear configuration error instead of falling back to a
  hardcoded model.
  Evidence: `src/abstractgateway/provider_defaults.py`, `src/abstractgateway/hosts/bundle_host.py`
- `ABSTRACTGATEWAY_TOOL_MODE`:
  - `approval` (default): execute safe tools locally; require explicit approval for dangerous/unknown tools
  - `passthrough`: require explicit approval for *all* tools (then execute in-process on resume)
  - `delegated`: do not execute tools; tool calls yield a durable `JOB` wait for external executors
  - `local` (or `local_all`): execute all tools inside the gateway process (dev only; higher risk)
  Evidence: `src/abstractgateway/hosts/bundle_host.py` (tool executor selection)

### Embeddings

The gateway exposes an embeddings API when the execution host has an explicit `embedding.text`
capability default and AbstractCore embedding deps are available.

Configure it through the same capability-default control plane used by Flow:

```bash
abstractgateway-config set-default embedding.text \
  --provider lmstudio \
  --model text-embedding-nomic-embed-text-v1.5 \
  --base-url http://127.0.0.1:1234/v1
```

In embedded deployments Gateway uses the local Core embedding manager. In split deployments it
delegates to the remote AbstractCore `/v1/embeddings` route so provider `base_url` is evaluated
from the Core host.

Evidence: `src/abstractgateway/embeddings_config.py`

### Prompt cache controls (provider-dependent)

Gateway prompt-cache endpoints are available when the AbstractCore integration
for the active provider/model exposes them. Remote providers usually provide
server-managed cache hints; local in-process providers can expose stronger
control-plane operations when installed in a custom runtime image.
Provider-level endpoints remain available for operators, and session-level
endpoints provide a deterministic gateway-owned namespace/key lifecycle for thin
apps without pretending unsupported providers have local KV state.

- `GET /api/gateway/prompt_cache/capabilities`
- `GET /api/gateway/prompt_cache/stats`
- `POST /api/gateway/prompt_cache/set`
- `POST /api/gateway/prompt_cache/update`
- `POST /api/gateway/prompt_cache/fork`
- `POST /api/gateway/prompt_cache/clear`
- `POST /api/gateway/prompt_cache/prepare_modules`
- `POST /api/gateway/blocs/upsert_text`
- `GET /api/gateway/blocs/record`
- `GET /api/gateway/blocs`
- `POST /api/gateway/blocs/delete`
- `GET /api/gateway/blocs/kv/manifest`
- `GET /api/gateway/blocs/kv/list`
- `POST /api/gateway/blocs/kv/ensure`
- `POST /api/gateway/blocs/kv/load`
- `POST /api/gateway/blocs/kv/delete`
- `POST /api/gateway/blocs/kv/prune`
- `GET /api/gateway/prompt_cache/saved`
- `POST /api/gateway/prompt_cache/save`
- `POST /api/gateway/prompt_cache/load`
- `GET /api/gateway/sessions/{session_id}/prompt_cache/status`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/prepare`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/rebuild`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/clear`

Session lifecycle responses distinguish `unsupported`, `keyed`, and
`local_control_plane` modes. Keyed providers receive a stable `runtime_hint`;
local-control-plane providers can prepare, clear, and rebuild when their
AbstractCore provider exposes those operations.

Treat the three prompt-cache surfaces separately:

- `/prompt_cache/*`: provider/model prompt-cache controls
- `/sessions/{session_id}/prompt_cache/*`: gateway-owned volatile session lifecycle
- `/blocs/*`: durable exact-reuse bloc/KV contract that returns `prompt_cache_binding`

The `saved` / `save` / `load` aliases are Runtime-backed host-local admin
operations. Local runtimes write under `<DATA_DIR>/prompt_cache_exports`; remote
and hybrid runtimes report `prompt_cache_local_only`.

### Multimodal provider/plugin controls

The base install already includes the Gateway HTTP/SSE server and the Runtime
multimodal integration layer. Direct Gateway routes for voice/audio, image/video,
and music become available when the corresponding lower-layer capability
packages are installed on the gateway host (or when Gateway is configured to
proxy to a remote AbstractCore server).

Local heavy engines remain explicit opt-ins in the provider packages; Gateway
does not implicitly install them.

- `output.text` capability route: default text model for bundle LLM nodes
- `OPENAI_COMPATIBLE_BASE_URL` / `OPENAI_COMPATIBLE_API_KEY`: OpenAI-compatible text endpoint for AbstractCore providers
  - Apple/MLX Docker deployments should point the lightweight Gateway container
    at host-native inference, for example
    `http://model-runner.docker.internal/engines/v1`,
    `http://host.docker.internal:1234/v1`, or
    `http://host.docker.internal:11434/v1`.
- `ABSTRACTGATEWAY_VISION_BACKEND` / `ABSTRACTGATEWAY_VISION_BASE_URL` / `ABSTRACTGATEWAY_VISION_API_KEY` / `ABSTRACTGATEWAY_VISION_MODEL_ID`: Gateway-scoped image backend settings. Legacy `ABSTRACTVISION_*` names are still accepted by the lower package.
- `ABSTRACTGATEWAY_VOICE_TTS_ENGINE` / `ABSTRACTGATEWAY_VOICE_STT_ENGINE`: Gateway-scoped voice engine settings. Legacy `ABSTRACTVOICE_*` names are still accepted by the lower package.
- `ABSTRACTGATEWAY_VOICE_TTS_MODEL` / `ABSTRACTGATEWAY_VOICE_STT_MODEL`: Gateway-scoped TTS/STT model defaults.
- `ABSTRACTGATEWAY_VOICE_REMOTE_BASE_URL` / `ABSTRACTGATEWAY_VOICE_REMOTE_API_KEY`: remote voice endpoint used by AbstractVoice.
- `GET /api/gateway/discovery/capabilities`: reports installed packages plus AbstractCore capability plugins for `voice`, `audio`, `vision`, and `music`; also returns `capabilities.contracts.version=1` with thin-client feature gates for AbstractFlow, AbstractAssistant, AbstractCode, shared run input/history endpoints, artifact search/import/export, direct voice/audio/image/video/music endpoints, workflow-backed image/video generation, and provider/session prompt-cache controls
- `GET /api/gateway/voice/voices`: proxies AbstractCore `/v1/audio/voices` when `ABSTRACTCORE_SERVER_BASE_URL` is configured; otherwise returns static Gateway/env voice descriptors.
- `GET /api/gateway/audio/speech/models`: proxies AbstractCore `/v1/audio/speech/models` when configured.
- `GET /api/gateway/audio/transcriptions/models`: proxies AbstractCore `/v1/audio/transcriptions/models` when configured.
- `GET /api/gateway/audio/music/providers`: proxies AbstractCore `/v1/audio/music/providers` when configured.
- `GET /api/gateway/audio/music/models`: proxies AbstractCore `/v1/audio/music/models` when configured.
- `GET /api/gateway/vision/provider_models`: proxies AbstractCore `/v1/vision/provider_models` when configured.
- `GET /api/gateway/vision/models`: reports locally known/cached AbstractVision model ids when the in-process capability path is available.
- `POST /api/gateway/runs/{run_id}/images/edit`: creates a durable Runtime child run for image-to-image edits and optional mask-guided edits.
- `POST /api/gateway/runs/{run_id}/videos/generate`: creates a durable Runtime child run for text-to-video and returns an artifact-backed video result.
- `POST /api/gateway/runs/{run_id}/videos/from_image`: creates a durable Runtime child run for image-to-video and returns an artifact-backed video result.
- `POST /api/gateway/runs/{run_id}/music/generate`: creates a durable Runtime child run and returns an artifact-backed music result for thin clients.

Core catalog proxy settings:

- `ABSTRACTCORE_SERVER_BASE_URL`: explicit Core server base URL for catalog proxying.
- `ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN` / `ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY`
  (or Core's `ABSTRACTCORE_AUTH_TOKEN` / `ABSTRACTCORE_SERVER_API_KEY`): Core server auth token.
  This is separate from Gateway auth.
- `ABSTRACTGATEWAY_CORE_CATALOG_TIMEOUT_S`: catalog proxy timeout (default `3.0` seconds).

## CLI flags

`abstractgateway --help` shows all subcommands (serve/runner/migrate/triage/…).

Most-used:
- `abstractgateway serve --host 127.0.0.1 --port 8080 [--no-runner] [--reload]`
  Evidence: `src/abstractgateway/cli.py`
- `abstractgateway runner` (worker only)
- `abstractgateway config status --json`
- `abstractgateway migrate --from=file --to=sqlite --data-dir <DIR> --db-path <FILE>`

## Related docs

- Getting started: [getting-started.md](./getting-started.md)
- FAQ: [faq.md](./faq.md)
- Security configuration: [security.md](./security.md)
- Deployment: [deployment.md](./deployment.md)
- API overview: [api.md](./api.md)
- Operator tooling env vars: [maintenance.md](./maintenance.md)
