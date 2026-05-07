# AbstractGateway — Configuration

AbstractGateway is configured primarily via **environment variables** (plus a few CLI flags).

## Install extras (recommended)

The base install (`pip install abstractgateway`) includes the runner + durable stores + CLI.

Optional extras (see `pyproject.toml`):
- `abstractgateway[http]`: FastAPI + Uvicorn (`abstractgateway serve`)
- `abstractgateway[server]`: turnkey server/container profile with AbstractRuntime multimodal support, AbstractCore remote providers, OpenAI-compatible text providers, workflow-backed AbstractVision image generation, direct Gateway voice/audio endpoints, media/tool helpers, token counting, provider-level prompt-cache helpers, and compression
- `abstractgateway[visualflow]`: VisualFlow JSON directory mode via `abstractflow`
- `abstractgateway[telegram]`: Telegram bridge dependencies (AbstractRuntime’s AbstractCore integration)
- `abstractgateway[voice]`: voice/audio endpoints (TTS + STT) via AbstractVoice and AbstractCore's voice/audio plugin extras
- `abstractgateway[vision]`: generative vision via AbstractCore's AbstractVision plugin
- `abstractgateway[multimodal]`: Runtime/Core multimodal profile without HTTP server deps
- `abstractgateway[all]`: batteries-included install (HTTP + tools + voice/audio + vision + media + visualflow)
- `abstractgateway[docs]`: MkDocs site tooling
- `abstractgateway[dev]`: local dev/test deps

Optional (required by some workflows/features):
- `abstractruntime[abstractcore]>=0.4.6`: required to execute bundle workflows that contain LLM/tool nodes (see `src/abstractgateway/hosts/bundle_host.py`) — already included by `abstractgateway[http]`
- `abstractruntime[multimodal]>=0.4.6`: required for Runtime-managed multimodal outputs through AbstractCore workflows — included by `abstractgateway[server]`, `abstractgateway[multimodal]`, and `abstractgateway[all]`
- `abstractcore[remote,media,tools,tokens,compression,vision,voice,audio]>=2.13.10`: recommended for server deployments that need hosted providers, OpenAI-compatible text provider routing, media parsing, tool helpers, token counting, provider-level prompt-cache controls, workflow-backed image generation, TTS, and STT — included by `abstractgateway[server]`
- `abstractagent`: required for Visual Agent nodes (bundle mode) — already included by `abstractgateway[http]`
- `abstractmemory[lancedb]` (or `abstractmemory` + `lancedb`): required for bundles that use `memory_kg_*` nodes

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

- `ABSTRACTGATEWAY_PROVIDER` / `ABSTRACTGATEWAY_MODEL`  
  Used as defaults for LLM execution in bundle mode. If missing, the gateway may try to infer defaults from flow JSON; otherwise it errors.  
  Evidence: `src/abstractgateway/hosts/bundle_host.py` (`needs_llm`, `_scan_flows_for_llm_defaults`)
- `ABSTRACTGATEWAY_TOOL_MODE`:
  - `approval` (default): execute safe tools locally; require explicit approval for dangerous/unknown tools
  - `passthrough`: require explicit approval for *all* tools (then execute in-process on resume)
  - `delegated`: do not execute tools; tool calls yield a durable `JOB` wait for external executors
  - `local` (or `local_all`): execute all tools inside the gateway process (dev only; higher risk)
  Evidence: `src/abstractgateway/hosts/bundle_host.py` (tool executor selection)

### Embeddings (optional)

The gateway can expose an embeddings API if AbstractCore embedding deps are available.

- `ABSTRACTGATEWAY_EMBEDDING_PROVIDER` / `ABSTRACTGATEWAY_EMBEDDING_MODEL`  
  Persisted under `<DATA_DIR>/gateway_embeddings.json` for stability across restarts.  
  Evidence: `src/abstractgateway/embeddings_config.py`

### Prompt cache controls (provider-dependent)

Gateway prompt-cache endpoints are available when the AbstractCore integration
for the active provider/model exposes them. Remote providers usually provide
server-managed cache hints; local in-process providers can expose stronger
control-plane operations when installed in a custom runtime image.
These endpoints are provider/model controls, not a Gateway-owned CachedSession
lifecycle API.

- `GET /api/gateway/prompt_cache/capabilities`
- `GET /api/gateway/prompt_cache/stats`
- `POST /api/gateway/prompt_cache/set`
- `POST /api/gateway/prompt_cache/update`
- `POST /api/gateway/prompt_cache/fork`
- `POST /api/gateway/prompt_cache/clear`
- `POST /api/gateway/prompt_cache/prepare_modules`
- `POST /api/gateway/prompt_cache/save` / `load` for supported local providers

### Multimodal provider/plugin controls

The `server`, `multimodal`, and `all` extras install the lightweight AbstractCore
plugin surface for generated images, generated voice, STT, and future music/video
capabilities. In 0.2.2, voice generation and transcription have direct Gateway
endpoints; generated images are exposed through Runtime/Core workflows rather
than a direct Gateway image-generation endpoint. Local heavy engines remain
explicit opt-ins in the provider packages.

- `ABSTRACTGATEWAY_PROVIDER` / `ABSTRACTGATEWAY_MODEL`: default text model for bundle LLM nodes
- `OPENAI_COMPATIBLE_BASE_URL` / `OPENAI_COMPATIBLE_API_KEY`: OpenAI-compatible text endpoint for AbstractCore providers
- `ABSTRACTVISION_BASE_URL` / `ABSTRACTVISION_API_KEY` / `ABSTRACTVISION_MODEL_ID`: OpenAI-compatible image endpoint used by AbstractVision
- `ABSTRACTVISION_BACKEND`: `openai` / `diffusers` / `sdcpp` (`openai` means OpenAI-compatible HTTP)
- `ABSTRACTVOICE_TTS_ENGINE` / `ABSTRACTVOICE_STT_ENGINE`: `auto`, local engines, or remote-compatible engines supported by AbstractVoice
- `ABSTRACTVOICE_REMOTE_BASE_URL` / `ABSTRACTVOICE_REMOTE_API_KEY`: remote voice endpoint used by AbstractVoice
- `GET /api/gateway/discovery/capabilities`: reports installed packages plus AbstractCore capability plugins for `voice`, `audio`, `vision`, and future `music`

## CLI flags

`abstractgateway --help` shows all subcommands (serve/runner/migrate/triage/…).

Most-used:
- `abstractgateway serve --host 127.0.0.1 --port 8080 [--no-runner] [--reload]`  
  Evidence: `src/abstractgateway/cli.py`
- `abstractgateway runner` (worker only)
- `abstractgateway migrate --from=file --to=sqlite --data-dir <DIR> --db-path <FILE>`

## Related docs

- Getting started: [getting-started.md](./getting-started.md)
- FAQ: [faq.md](./faq.md)
- Security configuration: [security.md](./security.md)
- Deployment: [deployment.md](./deployment.md)
- API overview: [api.md](./api.md)
- Operator tooling env vars: [maintenance.md](./maintenance.md)
