# AbstractGateway — Configuration

AbstractGateway is configured primarily via **environment variables** (plus a few CLI flags).

## Install extras (recommended)

The base install (`pip install abstractgateway`) is the remote-light server
profile: HTTP/SSE, durable stores, `AbstractRuntime`,
Runtime-owned provider/tool and multimodal support, AbstractAgent, AbstractFlow
compatibility (runs bundles produced by AbstractFlow; does not require the
`abstractflow` package), and AbstractMemory/LanceDB KG support. Local
sentence-transformer embeddings and hardware-local inference engines are
opt-in, so the base Linux install does not pull PyTorch/CUDA packages.

Remote embeddings are part of this base light profile. Configure
`embedding.text` for OpenAI, OpenRouter, Portkey, LM Studio, vLLM, another
OpenAI-compatible embeddings endpoint, or a remote AbstractCore server. The
`abstractgateway[embeddings]` extra is only for local HuggingFace/
sentence-transformer embeddings on the Gateway host.

Optional extras (see `pyproject.toml`):
- `abstractgateway[embeddings]`: local sentence-transformer embeddings for semantic KG queries
- `abstractgateway[apple]`: full native macOS Python profile with Apple-local engines and all non-NVIDIA framework capabilities; this is for native macOS, not Docker
- `abstractgateway[gpu]`: full native/container GPU profile with local GPU engines and all relevant framework capabilities; the NVIDIA Docker image uses this profile
- `abstractgateway[docs]`: MkDocs site tooling
- `abstractgateway[dev]`: local dev/test deps

Default dependency floors:
- `AbstractRuntime>=0.4.26`
- `abstractagent>=0.3.10`
- `AbstractMemory[lancedb]>=0.2.6`

Gateway's KG resolver targets AbstractMemory's TripleStore API. It does not use
the newer memory-agent API directly.

## Configuration helper

Gateway has a first-class configuration helper:

```bash
abstractgateway-config status
abstractgateway-config init --env-file .env
abstractgateway-config bootstrap-admin --print-token
abstractgateway config status --json
```

It reports Gateway auth/data/store/runtime defaults, Core-server handoff
configuration, memory-store selection, and package readiness. `init` writes a
private env file for server/operator deployments. Gateway Console (`/console`)
is the preferred place to configure provider connections, provider API keys,
endpoint base URLs, users, and Gateway/user defaults. Provider URLs and keys
belong to the Providers tab; the Multimodal Capabilities tab only chooses an
available provider and a discovered model.
`bootstrap-admin` is the non-interactive setup path used by Docker images:
when user auth is enabled, it ensures `default/admin` exists, stores only the
token hash in `auth/users.json`, and can write the raw bootstrap token to
`auth/bootstrap-admin-token` for first login.

## Core environment variables

### Paths + workflow source

- `ABSTRACTGATEWAY_DATA_DIR`: durable data directory (default: `./runtime`)  
  Evidence: `src/abstractgateway/config.py` (`GatewayHostConfig.from_env`)
- `ABSTRACTGATEWAY_FLOWS_DIR`: workflows directory. When unset, Gateway first
  uses the packaged shipped bundle directory containing `basic-agent`. If the
  shipped bundle is unavailable, Gateway fails clearly instead of starting with
  an empty default registry.
  Evidence: `src/abstractgateway/config.py`
- `ABSTRACTGATEWAY_WORKFLOW_SOURCE`: `bundle` (default) or `visualflow`  
  Evidence: `src/abstractgateway/service.py` (`create_default_gateway_service`)

### Authentication and user routing

The normal browser-console/browser-app path uses Gateway user auth:

- `ABSTRACTGATEWAY_USER_AUTH=1` or `ABSTRACTGATEWAY_AUTH_MODE=users`: enable
  file-backed user principals and per-principal runtime routing
- `abstractgateway serve`: when user auth is enabled, ensures `default/admin`
  exists and writes the first-login token to
  `<ABSTRACTGATEWAY_DATA_DIR>/auth/bootstrap-admin-token`

Legacy server/operator mode uses a Gateway bearer token:

- `ABSTRACTGATEWAY_AUTH_TOKEN`: single Gateway admin token
- `ABSTRACTGATEWAY_AUTH_TOKENS`: comma-separated Gateway admin tokens

That legacy bearer token maps to `local-admin` and is not accepted by browser
sign-in flows such as `/console` or AbstractFlow. User-auth mode resolves
Gateway user bearer tokens to principals and routes each principal to a separate
service/data plane:

- `ABSTRACTGATEWAY_USER_AUTH_AUTO=1`: compatibility mode that also enables
  user auth when the registry file already exists
- `ABSTRACTGATEWAY_USERS_FILE`: optional user registry path; default:
  `<ABSTRACTGATEWAY_DATA_DIR>/auth/users.json`
- `ABSTRACTGATEWAY_SESSIONS_FILE`: optional browser session registry path;
  default: `<ABSTRACTGATEWAY_DATA_DIR>/auth/sessions.json`
- `ABSTRACTGATEWAY_SESSION_TTL_S`: default browser session lifetime
- `ABSTRACTGATEWAY_REMEMBER_SESSION_TTL_S`: browser session lifetime when a
  browser app requests "remember me"
- `ABSTRACTGATEWAY_ADMIN_USES_DEFAULT_RUNTIME`: keep the default
  `default/admin` admin principal on the Gateway's base data plane when its
  `runtime_id` is `default` or `admin` (default: enabled)
- `GET /api/gateway/me`: returns the resolved principal and routing mode
- `/api/gateway/admin/users`: admin-only user list/create/read/update/delete
- `/api/gateway/admin/runtime-reservations`: admin-only retained runtime
  list/transfer/purge lifecycle
- `/console`: built-in same-origin Gateway Console for session sign-in with
  Gateway user + token, account/runtime summary, admin user management, optional
  account email metadata, token rotation, retained runtime transfer/purge, and
  multimodal capability defaults selected from available providers

User records include `tenant_id`, `user_id`, roles/scopes, enabled state, and a
`runtime_id`. The registry stores password-grade bearer-token hashes only.
Generated or rotated user tokens are returned once from the admin response.
Gateway rejects duplicate `runtime_id` values within the same tenant when users
are created or updated, preserving `1 user = 1 runtime` for independent hosted
users. Deleting a user reserves its retained runtime id. Admins must explicitly
purge retained runtime data before the id can be reused by another user, or
transfer the retained runtime to an existing same-tenant user.

When user auth is active, `src/abstractgateway/service.py` keeps normal users
isolated in a per-principal service directory:

```text
<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant_id>/<runtime_id>/runtime
<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant_id>/<runtime_id>/flows
```

The bootstrap `default/admin` admin principal is a local-setup compatibility
exception by default: with `ABSTRACTGATEWAY_ADMIN_USES_DEFAULT_RUNTIME=1`, it
uses the base Gateway data plane and bundle registry. That keeps the admin
connected to the default runtime and shipped `basic-agent` bundle while regular
users remain on `1 user = 1 runtime` routing.

Browser apps should exchange a Gateway user token for an opaque Gateway browser
session through `/api/gateway/session/login`; the raw bearer token should not be
kept in browser storage, and the login response body does not expose the session
id or CSRF token. Session-authenticated writes carry
`X-AbstractGateway-Session` plus `X-AbstractGateway-CSRF`, and
`/api/gateway/session/logout` revokes the session. Apps such as AbstractFlow,
AbstractCode, AbstractAssistant, and AbstractObserver should authenticate as the
current user/session in hosted mode. They should not share one app-server
Gateway token for all users.

### Runtime-scoped Core capability defaults

In hosted user-auth mode, `GET /api/gateway/config/capability-defaults` returns
the execution-host Core capability routes plus the Gateway/root baseline and
any defaults configured for the current Gateway principal. The bootstrap
`default/admin` principal edits the Gateway baseline when it uses the default
runtime. Normal user writes to
`PUT /api/gateway/config/capability-defaults/{kind}/{modality}` are stored under
that principal's Gateway data plane as a Core config file and override the
Gateway baseline only for that user:

```text
$ABSTRACTGATEWAY_DATA_DIR/config/abstractcore.json
$ABSTRACTGATEWAY_DATA_DIR/users/<tenant>/<runtime>/runtime/config/abstractcore.json
```

This lets operators set a Gateway default and lets hosted users choose
remote-provider defaults for their own runtime without mutating the operator's
global AbstractCore config or other users. The route schema, normalization, and
file format come from AbstractCore capability-default contracts. Gateway no
longer reads or writes `config/capability_defaults.json`; existing overlay
files are ignored. Recreate those defaults with
`abstractgateway-config set-default ...`. Provider API keys and raw secrets are
not returned by these routes. Use Gateway provider connections when a route
default needs an API key or custom base URL.

Gateway model discovery delegates to AbstractRuntime's AbstractCore discovery
facade. LLM and embedding default pickers can filter models with Core route keys
such as `capability_route=input.image,output.text` or
`capability_route=embedding.text`. Generated image/video/voice/sound/music
defaults continue to use their capability plugin catalogs so provider readiness,
download/setup state, and backend-specific metadata do not get written into the
raw Core model registry.

CLI examples:

```bash
# Gateway baseline Core default
abstractgateway-config set-default input.text \
  --provider endpoint:openai-prod \
  --model gpt-4.1

# One user's runtime Core override
abstractgateway-config set-default input.text \
  --scope user \
  --tenant default \
  --user alice \
  --provider endpoint:alice-openai \
  --model gpt-4.1

abstractgateway-config defaults --scope user --user alice
```

`input.text` is the canonical text LLM route. `output.text` is reported as a
read-only derived view of `input.text`, and CLI/API writes to `output.text` are
canonicalized to `input.text` for compatibility. `input.image` is a fallback
image-understanding route only: when the selected `input.text` model is known
from AbstractCore model capabilities to accept image input, the console marks
`input.image` as covered by `input.text` and disables separate editing.
`input.video` follows the same coverage model when the text model can handle
visual frames, but it remains overrideable so operators can choose a dedicated
video/VLM route. `input.voice` is the speech-to-text fallback route; if it is
not configured and the selected text model cannot accept audio natively,
Gateway/Core fail clearly instead of using a hidden installed STT backend.
`input.sound` is for non-speech audio understanding and is not used as STT.
`input.music` is the corresponding music-audio understanding route. `input.sound`
and `input.music` may be shown as covered by `input.text` only when the selected
text model is known to accept those native inputs, and both rows remain
overrideable.
Audio-language candidates such as `qwen3-omni-30b-a3b-instruct`,
`qwen3-omni-30b-a3b-captioner`, `qwen2.5-omni-7b`, and
`qwen2-audio-7b-instruct` are registry-known options when the configured
provider can serve them. Qwen3.6 text/image/video defaults should not be treated
as sound or music understanding models.

### Provider connections

Gateway Console and `POST /api/gateway/config/provider-endpoint-profiles` let
signed-in users define reusable provider connections through a guided setup
flow for `openai`, `anthropic`, `openrouter`, `portkey`, `lmstudio`, `ollama`,
or `openai-compatible`. A connection includes a stable id, display name,
description, optional base URL, optional API key, and optional advanced model
allowlist. The raw API key is write-only: responses include only `api_key_set`
and a short fingerprint. AbstractCore owns model capability metadata, so normal
setup does not ask users to classify models manually.

The console's **Test** action calls the selected provider through
`POST /api/gateway/config/provider-endpoint-profiles/discover-models` and
previews model discovery before saving. Leave the advanced model restriction
empty to keep live discovery active, or select one or more models to store a
fixed allowlist. The **Multimodal Capabilities** tab shows configured provider
connections and direct providers that are already usable from scoped
AbstractCore config or environment variables. It does not collect endpoint base
URLs or API keys. Reachable default local servers such as LM Studio and Ollama
also appear automatically when Gateway can discover models from their
configured/default endpoint.

Enabled profiles appear in `GET /api/gateway/discovery/providers` as virtual
provider ids such as `endpoint:office-vllm`. Direct configured providers such
as `openai` or `anthropic` also appear automatically when their required API
key is available from scoped Core config or process environment. Use those
provider ids in Flow nodes or Gateway capability defaults. At runtime the
Gateway host resolves virtual providers to the real provider family, base URL,
and API key for the transient AbstractRuntime call; direct providers use the
scoped Core config/environment already available to the execution host.
Workflow JSON and browser storage do not contain the raw secret. Normal users
can manage user-scoped profiles. Gateway-scoped profiles require an admin
principal.

The console **Sandbox** tab reuses this configuration. It tests the selected
multimodal capability default rather than an ad hoc provider/model pair. Text
chat uses the configured text route, and generated media tests use configured
routes such as `output.image`, `output.voice`, `output.sound`, `output.music`,
or `output.video`. The Sandbox renders generated images, videos, voice, sound,
and music artifacts inline when the route completes, while keeping artifact
links available for opening the raw content. Text chat can include uploaded
attachments such as images, audio, video, PDFs, Markdown, or text documents.
Uploaded attachments are stored as Gateway artifacts and then materialized by
Runtime into provider-ready media for AbstractCore, so vision-capable
OpenAI-compatible text routes receive image uploads as native multimodal
`image_url` content. Sandbox text turns also send bounded browser-local
grounding context, including local datetime, timezone, timezone offset, and
locale. Runtime may use that browser context for prompt grounding only; it keeps
server-derived context as provenance and never uses browser metadata for auth,
runtime routing, or credential selection. Country grounding is inferred from the
browser timezone when possible, with locale only as a fallback.

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

- `input.text` capability route
  Default text route for LLM execution and Gateway LLM helper endpoints. Configure it through
  `abstractgateway-config set-default input.text ...` or
  `abstractcore config set-default input.text ...`.
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
capability default. Remote/provider-backed embeddings work with the base
remote-light install; local HuggingFace/sentence-transformer embeddings require
`abstractgateway[embeddings]`.

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

- `input.text` capability route: default text model for bundle LLM nodes
- `OPENAI_BASE_URL` / `OPENAI_API_KEY`: generic OpenAI-compatible text endpoint for AbstractCore providers
  - Apple/MLX Docker deployments should point the lightweight Gateway container
    at host-native inference, for example
    `http://model-runner.docker.internal/engines/v1`,
    `http://host.docker.internal:1234/v1`, or another `/v1` endpoint.
- `LMSTUDIO_BASE_URL` / `OLLAMA_BASE_URL`: named local endpoint providers for
  LM Studio and Ollama model discovery/routing from inside the Gateway container.
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
- `POST /api/gateway/runs/{run_id}/images/generate`: creates a durable Runtime child run for text-to-image and returns an artifact-backed image result. Optional `size`/`width`/`height` values are passed through only when the client supplies them.
- `POST /api/gateway/runs/{run_id}/images/edit`: creates a durable Runtime child run for image-to-image edits and optional mask-guided edits. Optional `size`/`width`/`height` values are passed through only when the client supplies them.
- `POST /api/gateway/runs/{run_id}/videos/generate`: creates a durable Runtime child run for text-to-video and returns an artifact-backed video result.
- `POST /api/gateway/runs/{run_id}/videos/from_image`: creates a durable Runtime child run for image-to-video and returns an artifact-backed video result.
- `POST /api/gateway/runs/{run_id}/music/generate`: creates a durable Runtime child run and returns an artifact-backed music result for thin clients.

Direct image, image-edit, text-to-video, and image-to-video child runs advertise
`event_name=abstract.progress`. Thin clients should stream the returned
`child_run_id` ledger and render progress when the backend reports it; image
backends that do not expose step progress still emit at least a start record and
then the final artifact.

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
