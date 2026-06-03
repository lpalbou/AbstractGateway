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
- **AbstractRuntime + transitive capability packages** (required by the default server install): Runtime owns the LLM/tool/media integration boundary; Gateway uses its discovery/run facades for prompt-cache controls, generated and edited image/video plus voice/audio/music capabilities, and KG-backed bundle execution (`src/abstractgateway/hosts/bundle_host.py`)
- Higher-level UIs (optional): AbstractFlow (authoring/bundling), AbstractCode / AbstractObserver / thin clients (rendering + operations)

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

## Quickstart (HTTP server, bundle mode)

```bash
pip install abstractgateway

export ABSTRACTGATEWAY_DATA_DIR="$PWD/runtime/gateway"

# Optional: set only for a custom bundle registry. When unset, Gateway uses
# the packaged shipped bundle directory containing basic-agent.
# export ABSTRACTGATEWAY_FLOWS_DIR="/path/to/bundles"

# User auth is the normal browser-console/browser-app path.
export ABSTRACTGATEWAY_USER_AUTH=1
# Browser-origin allowlist (glob patterns). Default allows localhost; customize when exposing remotely.
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="http://localhost:*,http://127.0.0.1:*"

abstractgateway serve --host 127.0.0.1 --port 8080
```

OpenAPI docs (Swagger UI): `http://127.0.0.1:8080/docs`

Smoke checks:

```bash
curl -sS "http://127.0.0.1:8080/api/health"

curl -sS -H "Authorization: Bearer $(cat "$ABSTRACTGATEWAY_DATA_DIR/auth/bootstrap-admin-token")" \
  "http://127.0.0.1:8080/api/gateway/bundles"
```

## Hosted user auth

`ABSTRACTGATEWAY_AUTH_TOKEN` is a legacy Gateway-level bearer token for
server/operator access. Browser apps and `/console` should use file-backed user
principals and one runtime/data plane per user:

```bash
export ABSTRACTGATEWAY_USER_AUTH=1
abstractgateway serve --host 127.0.0.1 --port 8080
```

On first start, Gateway creates `default/admin`, writes the browser-login token
to `<ABSTRACTGATEWAY_DATA_DIR>/auth/bootstrap-admin-token`, and prints the token
when bound to a loopback host. Use user `admin` plus that `agw_...` token in
`/console`, AbstractFlow, AbstractCode Web, or AbstractObserver. Admins manage
users through `/api/gateway/admin/users`; generated user bearer tokens are
returned once and stored only as hashes under
`<ABSTRACTGATEWAY_DATA_DIR>/auth/users.json`. User clients call
`GET /api/gateway/me` after connecting to confirm the resolved principal and
routing mode. Gateway rejects duplicate runtime ids within the same tenant, so
the default hosted model remains `1 user = 1 runtime`. Deleting a user reserves
the retained runtime id; admins can explicitly purge retained runtime data or
transfer it to an existing same-tenant user. See
[docs/security.md](docs/security.md) for the current hosted isolation boundary
and remaining operator-route hardening work.

Gateway also serves a built-in control-plane console at `/console`. The console
uses the same browser-session contract as hosted apps: sign in with a Gateway
user id and its token. Because the console is served by Gateway, it uses the
current origin and does not ask for a Gateway URL. You can then manage the
current account, admin-only user records with optional email metadata, retained
runtime reservations, provider connections, and multimodal capability defaults
selected from available providers without storing the bearer token in browser
storage. Provider endpoint URLs/API keys are configured in the Providers tab;
the Multimodal Capabilities tab only selects provider/model pairs. Direct
providers such as `openai` and `anthropic` appear automatically when their
required keys are already available from scoped Core config or environment.
Reachable default local servers such as LM Studio (`http://localhost:1234/v1`)
and Ollama (`http://localhost:11434`) are also surfaced automatically when
Gateway can discover models from them. The Sandbox tab runs quick smoke tests
against the selected multimodal capability defaults in a chat surface, including
text chat, drag-and-drop attachments, inline image/video previews, and audio
players for voice, sound, and music artifacts.
Input fallback routes are explicit: `input.voice` selects the STT backend for
speech attachments, and `input.video` selects an overrideable video/VLM fallback
when the text route cannot or should not handle frames directly. If those routes
are unconfigured and the primary text model lacks native support, Gateway/Core
return a configuration error instead of silently probing installed packages.

Browser apps should not keep user bearer tokens. They exchange the user token at
`POST /api/gateway/session/login` for an opaque Gateway browser session and use
that session for proxied Gateway calls. The login response body does not return
the session id or CSRF token. Session writes require the Gateway CSRF token, and
`POST /api/gateway/session/logout` revokes the session.

## Docker server

Release images are published to GHCR. The default image is the light,
portable server image:

```bash
docker pull ghcr.io/lpalbou/abstractgateway:0.2.25
```

NVIDIA hosts can try the experimental full GPU image when local
vLLM/HuggingFace/Diffusers engines are wanted. This image is published
best-effort until it has a real CUDA build and smoke gate:

```bash
docker pull ghcr.io/lpalbou/abstractgateway:0.2.25-gpu
```

Legacy `abstractgateway-server` and `abstractgateway-server-nvidia` GHCR aliases
are still published for existing deployments; new deployments should use
`abstractgateway`.

The image installs the base `abstractgateway` package: HTTP server,
`AbstractRuntime`, Runtime-owned provider/tool and
multimodal facades, OpenAI-compatible text/media providers,
provider/session prompt-cache helpers, AbstractMemory/LanceDB KG support,
AbstractAgent, and AbstractFlow compatibility. Local sentence-transformer
embeddings and hardware-local inference engines are explicit extras so the
light server image does not pull PyTorch/CUDA runtime packages. Remote text
embeddings remain part of the light profile through the `embedding.text`
capability route: point it at OpenAI, OpenRouter, Portkey, LM Studio, vLLM,
any OpenAI-compatible embeddings endpoint, or a remote AbstractCore server.

AbstractFlow note:
- You do **not** need the `abstractflow` Python package to run `.flow` bundles (bundle mode). You only need it to author bundles. VisualFlow directory mode was intentionally removed from the gateway to keep the dependency direction clean.

```bash
docker run --rm --name abstractgateway \
  -p 8080:8080 \
  -e ABSTRACTGATEWAY_DATA_DIR=/data \
  -e ABSTRACTGATEWAY_USER_AUTH=1 \
  -e LMSTUDIO_BASE_URL="http://host.docker.internal:1234/v1" \
  -v "$PWD/runtime:/data" \
  ghcr.io/lpalbou/abstractgateway:latest
```

On first start, the container creates `default/admin` and writes the admin user
token to `runtime/auth/bootstrap-admin-token`. Use that token in `/console`,
then rotate it or create named users from the console.

Configure framework model defaults through execution-host capability routes:

```bash
docker exec abstractgateway abstractgateway-config set-default input.text \
  --provider lmstudio \
  --model your-model \
  --base-url http://host.docker.internal:1234/v1
```

In user-auth mode this writes the Gateway baseline Core config at
`/data/config/abstractcore.json`. Per-user runtime overrides use the same Core
schema under `/data/users/<tenant>/<runtime>/runtime/config/abstractcore.json`;
use `abstractgateway-config set-default --scope user --user alice ...` for
operator-side scripting.

`output.text` is a compatibility alias for this same text route. Gateway reports
it as a read-only view of `input.text`, so LLM text input and output do not drift
to different default models.

On Apple Silicon, keep Metal/MLX inference native on macOS and run the
lightweight Gateway container as the transport/control plane. Point
`OPENAI_BASE_URL` at a generic host-native OpenAI-compatible endpoint such as
Docker Model Runner (`http://model-runner.docker.internal/engines/v1`) or
`mlx_lm.server` on a host port. For named providers, use
`LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1` or
`OLLAMA_BASE_URL=http://host.docker.internal:11434`.
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
- `POST /api/gateway/runs/{run_id}/videos/generate`
- `POST /api/gateway/runs/{run_id}/videos/from_image`
- `POST /api/gateway/runs/{run_id}/music/generate`
- `GET /api/gateway/voice/voices`
- `GET /api/gateway/audio/speech/models`
- `GET /api/gateway/audio/transcriptions/models`
- `GET /api/gateway/audio/music/providers`
- `GET /api/gateway/audio/music/models`
- `GET /api/gateway/vision/provider_models`
- `GET /api/gateway/vision/models`
- `/api/gateway/artifacts/search` cross-run/session/run artifact search with
  modality, content-type, text, and tag filters
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
- Generated images and videos are available to Runtime workflows through
  Runtime's media backend integrations, and the direct Gateway image/video
  routes use the same Runtime/Core output-selector contracts. Image,
  image-edit, text-to-video, and image-to-video direct routes expose child-run
  progress through `abstract.progress` ledger records; image progress is
  best-effort and may be limited to start/complete for backends that do not
  report step progress.
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
Agent, Flow compatibility, Runtime-owned provider/tool and multimodal facades,
and LanceDB-backed Memory. It intentionally excludes local sentence-transformer
embeddings and hardware-local inference engines so Linux installs do not pull
PyTorch/CUDA packages. Remote embeddings and remote multimodal input/output
still work in this profile through hosted providers, OpenAI-compatible
endpoints, or a remote AbstractCore server.

```bash
pip install abstractgateway
```

### Optional extras

- `abstractgateway[apple]`: full native macOS Python profile with Apple-local engines and all non-NVIDIA framework capabilities
- `abstractgateway[gpu]`: full local GPU profile with vLLM/HuggingFace, local Diffusers image generation, local voice engines, music, and KG memory; this is also the NVIDIA Docker install profile
- `abstractgateway[embeddings]`: local sentence-transformer embeddings for semantic KG queries
- `abstractgateway[docs]`: MkDocs site tooling
- `abstractgateway[dev]`: local test/dev deps

KG memory nodes use Gateway's memory resolver. The default durable/vector
backend is LanceDB; `memory` is process-local dev/test storage, and `sqlite` is
structured-only when the installed AbstractMemory build exposes
`SQLiteTripleStore`. A fresh persistent store is still reported as available
when the backend resolves; empty queries return empty results instead of hiding
KG authoring surfaces.

Gateway has a first-class config helper:

```bash
abstractgateway-config status
abstractgateway config init --env-file .env
```

For details on capability route defaults, store backends, and workflow sources, see [docs/configuration.md](docs/configuration.md).

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
