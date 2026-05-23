# AbstractGateway

AbstractGateway is a **deployable Run Gateway host** for AbstractRuntime runs:
- start durable runs
- accept a durable command inbox
- replay/stream a durable ledger (replay-first)
- enforce a security baseline (token + origin allowlist + limits)

This decouples the gateway service from any specific UI (AbstractFlow, AbstractCode, web/PWA thin clients).

Start here: [docs/getting-started.md](docs/getting-started.md)

## AbstractFramework ecosystem

AbstractGateway is part of the **AbstractFramework** ecosystem:

- **AbstractRuntime** (required): durable run model + workflow registry + stores (`pyproject.toml`, `src/abstractgateway/runner.py`)
- **AbstractRuntime + transitive capability packages** (required by the default server install): Runtime owns the LLM/tool/media integration boundary; Gateway uses its discovery/run facades for prompt-cache controls, generated and edited image plus voice/audio/music capabilities, and KG-backed bundle execution (`src/abstractgateway/hosts/bundle_host.py`)
- Higher-level UIs (optional): AbstractFlow (authoring/bundling), AbstractCode / AbstractObserver / thin clients (rendering + operations)

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

## Quickstart (HTTP server, bundle mode)

```bash
pip install abstractgateway

export ABSTRACTGATEWAY_FLOWS_DIR="/path/to/bundles"   # *.flow dir (or a single .flow file)
export ABSTRACTGATEWAY_DATA_DIR="$PWD/runtime/gateway"

# Required by default: the server refuses to start without a token.
export ABSTRACTGATEWAY_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
# Browser-origin allowlist (glob patterns). Default allows localhost; customize when exposing remotely.
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

## Docker server

Release images are published to GHCR. The default image is the light,
portable server image:

```bash
docker pull ghcr.io/lpalbou/abstractgateway-server:0.2.18
```

NVIDIA hosts can try the experimental full GPU image when local
vLLM/HuggingFace/Diffusers engines are wanted. This image is published
best-effort until it has a real CUDA build and smoke gate:

```bash
docker pull ghcr.io/lpalbou/abstractgateway-server-nvidia:0.2.18
```

The image installs the base `abstractgateway` package: HTTP server,
`AbstractRuntime[multimodal,mcp-worker]`, Runtime-owned provider/media/tool
facades, OpenAI-compatible text providers, workflow-backed and direct image,
voice, audio, and music routes surfaced through Runtime, provider/session prompt-cache
helpers, AbstractMemory/LanceDB KG support, AbstractAgent, and AbstractFlow
compatibility.

AbstractFlow note:
- You do **not** need the `abstractflow` Python package to run `.flow` bundles (bundle mode). You only need it to author bundles. VisualFlow directory mode was intentionally removed from the gateway to keep the dependency direction clean.

```bash
export ABSTRACTGATEWAY_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

docker run --rm --name abstractgateway-server \
  -p 127.0.0.1:8080:8080 \
  -e ABSTRACTGATEWAY_AUTH_TOKEN="$ABSTRACTGATEWAY_AUTH_TOKEN" \
  -e ABSTRACTGATEWAY_PROVIDER="openai-compatible" \
  -e ABSTRACTGATEWAY_MODEL="your-model" \
  -e OPENAI_COMPATIBLE_BASE_URL="http://host.docker.internal:1234/v1" \
  -v "$PWD/runtime/gateway:/data/gateway" \
  -v "$PWD/flows/bundles:/data/flows:ro" \
  ghcr.io/lpalbou/abstractgateway-server:0.2.18
```

On Apple Silicon, keep Metal/MLX inference native on macOS and run the
lightweight Gateway container as the transport/control plane. Point
`OPENAI_COMPATIBLE_BASE_URL` at a host-native OpenAI-compatible endpoint such
as Docker Model Runner (`http://model-runner.docker.internal/engines/v1`), LM
Studio (`http://host.docker.internal:1234/v1`), Ollama
(`http://host.docker.internal:11434/v1`), or `mlx_lm.server` on a host port.
For native non-Docker installs with local engines, use
`pip install "abstractgateway[apple]"` on Apple Silicon, and
`pip install "abstractgateway[gpu]"` on GPU workstations or NVIDIA Docker builds.
For a minimal Apple-local Gateway + Flow setup, see
[docs/apple-local-gateway-flow.md](docs/apple-local-gateway-flow.md).

Compose and deployment details: [docs/deployment.md](docs/deployment.md).

## Current capability scope

Current direct Gateway APIs:
- `GET /api/gateway/runs/{run_id}/input_data`
- `GET /api/gateway/runs/{run_id}/history_bundle`
- `POST /api/gateway/runs/{run_id}/voice/tts`
- `POST /api/gateway/runs/{run_id}/audio/transcribe`
- `POST /api/gateway/runs/{run_id}/images/generate`
- `POST /api/gateway/runs/{run_id}/images/edit`
- `POST /api/gateway/runs/{run_id}/music/generate`
- `GET /api/gateway/voice/voices`
- `GET /api/gateway/audio/speech/models`
- `GET /api/gateway/audio/transcriptions/models`
- `GET /api/gateway/audio/music/providers`
- `GET /api/gateway/audio/music/models`
- `GET /api/gateway/vision/provider_models`
- `GET /api/gateway/vision/models`
- `/api/gateway/prompt_cache/*` provider/model operator controls
- `/api/gateway/prompt_cache/saved|save|load` Runtime-backed host-local export/import admin aliases
- `/api/gateway/sessions/{session_id}/prompt_cache/*` session lifecycle controls
- `/api/gateway/kg/query` with configurable `lancedb` or in-memory AbstractMemory stores, plus `sqlite` when the installed AbstractMemory build exposes `SQLiteTripleStore`
- `/api/gateway/discovery/capabilities` package, plugin, and thin-client contract discovery

Discovery note:
- the capability contract is versioned and stable for endpoint discovery and
  feature gating
- provider/model/voice catalog routes now add a stable Gateway-owned envelope:
  `catalog.contract = gateway_catalog_v1` plus canonical `items`
- the shared thin-client contract now also exposes `common.readiness` as a
  compact Gateway-owned surface summary derived from endpoint descriptors
- legacy fields such as `models`, `providers`, `provider_models`, `profiles`,
  and `voices` remain in place for compatibility
- richer deployment/readiness truth is still separate from the catalog
  envelope and depends on lower-layer Runtime/Core surfaces

Workflow/Core-backed capabilities:
- Generated images are available to Runtime workflows through Runtime's image
  backend integrations, and the direct Gateway image routes use the same
  Runtime/Core image-generation and image-edit contracts.
- Generated music is available through Gateway's direct Runtime-backed child-run
  route, with provider/model discovery exposed through Gateway capability
  contracts and music catalog endpoints for higher apps.
- Catalog routes now return a canonical `items` array and a `catalog` metadata
  block so higher apps can stop parsing route-local payload variants.
- Audio transcription is available through a direct Runtime-backed child-run
  route, and the capability contract also exposes `voice.listen` as a
  host-capture command surface for higher apps that record locally before
  emitting events or uploading audio.
- Prompt-cache support depends on the active provider/model. Session lifecycle
  routes provide Gateway-owned naming and orchestration, not a provider-
  independent local KV cache.
- Prompt-cache export/import admin remains local-only. Local runtimes keep those
  artifacts under the Gateway data dir; remote and hybrid runtimes return a
  structured `prompt_cache_local_only` response.

## Client contract (replay-first)

- Clients **start runs**: `POST /api/gateway/runs/start`
- Clients can **schedule runs** (bundle mode): `POST /api/gateway/runs/schedule`
- Clients **act** by submitting durable commands: `POST /api/gateway/commands`
  - supported types: `pause|resume|cancel|emit_event|update_schedule|compact_memory`
- Clients **render** by replaying/streaming the durable ledger:
  - replay: `GET /api/gateway/runs/{run_id}/ledger?after=...`
  - stream (SSE): `GET /api/gateway/runs/{run_id}/ledger/stream?after=...`

See [docs/api.md](docs/api.md) for curl examples and the live OpenAPI spec (`/openapi.json`).

## Install

### Base remote-light server

Requires Python `>=3.10` (see `pyproject.toml`).

The base install is the remote-light HTTP/SSE server: Gateway, Runtime,
Agent, Flow compatibility, Runtime-owned provider/tool/media facades, and
LanceDB-backed Memory.

```bash
pip install abstractgateway
```

### Optional extras

- `abstractgateway[apple]`: full native macOS Python profile with Apple-local engines and all non-NVIDIA framework capabilities
- `abstractgateway[gpu]`: full local GPU profile with vLLM/HuggingFace, local Diffusers image generation, local voice engines, music, and KG memory; this is also the NVIDIA Docker install profile
- `abstractgateway[server]`, `[http]`, `[multimodal]`, `[memory]`, `[voice]`, `[vision]`, and `[all]`: compatibility aliases; the base package already includes the remote-light server stack
- `abstractgateway[server-nvidia]`: compatibility alias for the GPU profile used by older NVIDIA Docker commands
- `abstractgateway[telegram]` and `[visualflow]`: compatibility aliases; Telegram bridge and VisualFlow publishing use base install dependencies
- `abstractgateway[docs]`: MkDocs site tooling
- `abstractgateway[dev]`: local test/dev deps

KG memory nodes use Gateway's memory resolver. The default durable/vector
backend is LanceDB; `memory` is process-local dev/test storage, and `sqlite` is
structured-only when the installed AbstractMemory build exposes
`SQLiteTripleStore`.

Gateway has a first-class config helper:

```bash
abstractgateway-config status
abstractgateway config init --env-file .env
```

For details on `ABSTRACTGATEWAY_PROVIDER`/`MODEL`, store backends, and workflow sources, see [docs/configuration.md](docs/configuration.md).

## Creating a `.flow` bundle (authoring)

Use AbstractFlow to pack a bundle:

```bash
abstractflow bundle pack /path/to/root.json --out /path/to/bundles/my.flow --flows-dir /path/to/flows
```

See [docs/getting-started.md](docs/getting-started.md) for running, split API/runner, and file→SQLite migration.

## Docs

Published docs site: https://www.lpalbou.info/AbstractGateway/

### Project docs

- Changelog: [CHANGELOG.md](CHANGELOG.md) (compat: `CHANGELOD.md`)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security policy (vulnerability reporting): [SECURITY.md](SECURITY.md)
- Acknowledgments: [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) (compat: `ACKNOWLEDMENTS.md`)

### Package docs

- Docs index: [docs/README.md](docs/README.md)
- Getting started: [docs/getting-started.md](docs/getting-started.md)
- FAQ: [docs/faq.md](docs/faq.md)
- Architecture: [docs/architecture.md](docs/architecture.md)
- Configuration: [docs/configuration.md](docs/configuration.md)
- Deployment: [docs/deployment.md](docs/deployment.md)
- API overview: [docs/api.md](docs/api.md)
- Security: [docs/security.md](docs/security.md)
- Operator tooling (optional): [docs/maintenance.md](docs/maintenance.md)
