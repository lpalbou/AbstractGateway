# AbstractGateway

AbstractGateway is a **deployable Run Gateway host** for AbstractRuntime runs,
and the control plane of an AbstractFramework installation:

- start durable runs, accept a durable command inbox, and replay or stream a
  durable ledger (replay-first);
- enforce a security baseline: user accounts, browser sessions, an origin
  allowlist, request limits and an audit log;
- manage users, providers, capability defaults, local engines, model
  downloads, browser apps and network exposure from a web console
  (`/console`), a terminal console, the CLI or a desktop tray icon.

Clients (AbstractFlow, AbstractCode, AbstractObserver, AbstractContinuum,
AbstractEntity, AbstractAssistant, scripts) talk to the gateway over HTTP, so
none of them depends on the others.

Start here: [docs/first-run.md](docs/first-run.md) on your own machine, or
[docs/getting-started.md](docs/getting-started.md) for an explicit setup.

## AbstractFramework ecosystem

AbstractGateway is part of the **AbstractFramework** ecosystem:

- **AbstractRuntime** (required): durable run model + workflow registry + stores (`pyproject.toml`, `src/abstractgateway/runner.py`)
- **AbstractCore, AbstractAgent, AbstractMemory** (installed with the gateway): Runtime owns the LLM/tool/media integration boundary; Gateway uses its discovery and run facades for prompt-cache controls, generated and edited media, voice, audio and music, and KG-backed bundle execution (`src/abstractgateway/hosts/bundle_host.py`)
- Apps (optional): AbstractFlow (authoring/bundling), AbstractCode, AbstractObserver, AbstractContinuum, AbstractEntity, AbstractAssistant

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

## Quickstart: your own machine

```bash
pip install abstractgateway
abstractgateway serve
```

On a first run with nothing configured, `serve` binds `127.0.0.1:8080`, turns
on user auth, creates the admin user, and prints a one-time link
(`http://127.0.0.1:8080/console#claim=...`). Open it in a browser on the same
machine: you are signed in as the admin and a short first-run guide helps you
pick a local engine, a default model and the browser apps. A new link:
`abstractgateway claim --open`. Start the gateway at login:
`abstractgateway service install`. See [docs/first-run.md](docs/first-run.md).

### Local models and engines

The console's **Models** and **Engines** tabs (and the matching commands)
install a local engine and download a model that fits this machine, without a
terminal. They are AbstractCore's model browser and engine installer, shown
inside the gateway:

```bash
abstractgateway engines status --probe          # Ollama, LM Studio, MLX, llama.cpp, ...
abstractgateway engines install ollama --dry-run # the exact command, nothing runs
abstractgateway models catalog --fits           # models that fit this machine
abstractgateway models download ollama qwen3:8b
abstractgateway models list                     # installed models with sizes
```

The commands talk to the running gateway (admin rules and audit log apply);
add `--local` to run them in-process instead. Engine installs run on the
gateway host and are on by default for a loopback gateway, and for someone at
the gateway machine whatever it listens on
([`allow_engine_install`](docs/configuration.md#allow_engine_install)). See
[docs/engines.md](docs/engines.md), [docs/model-downloads.md](docs/model-downloads.md)
and [docs/console.md](docs/console.md).

### Browser apps, network access and the tray

```bash
abstractgateway apps install observer --launch   # Flow, Code, Observer, Continuum, Entity, Assistant
abstractgateway apps open observer               # a one-time signed-in link
abstractgateway network set lan                  # let your local network reach the gateway
abstractgateway network restart                  # apply it now
```

The gateway installs Node.js when needed, installs the browser apps from npm,
runs them and opens them already signed in ([docs/apps.md](docs/apps.md)). The
network setting decides who can reach the gateway: `localhost`, `lan` or
`internet`
([docs/configuration.md](docs/configuration.md#network-exposure-localhost--local-network--internet)).
With `pip install "abstractgateway[tray]"`, `serve` also shows a menu bar /
system tray icon ([docs/tray.md](docs/tray.md)).

## Quickstart (HTTP server, bundle mode, explicit configuration)

```bash
pip install abstractgateway

export ABSTRACTGATEWAY_DATA_DIR="$PWD/runtime/gateway"

# Optional: set only for a custom bundle registry. When unset, Gateway uses
# the packaged shipped bundle directory containing basic-agent.
# export ABSTRACTGATEWAY_FLOWS_DIR="/path/to/bundles"

# User accounts: the sign-in path for the console and the browser apps.
export ABSTRACTGATEWAY_USER_AUTH=1

abstractgateway serve --host 127.0.0.1 --port 8080
```

OpenAPI docs (Swagger UI): `http://127.0.0.1:8080/docs`

Smoke checks:

```bash
curl -sS "http://127.0.0.1:8080/api/health"

curl -sS -H "Authorization: Bearer $(cat "$ABSTRACTGATEWAY_DATA_DIR/auth/bootstrap-admin-token")" \
  "http://127.0.0.1:8080/api/gateway/bundles"
```

That last call lists the workflows a fresh install already serves — including a
verify-gated coding agent (`coding-agent`), `deep-research`, and
`co-scientist`, alongside the default `basic-agent`. See
[docs/shipped-workflows.md](docs/shipped-workflows.md).

## User accounts and the console

With user accounts on (the default of a plain `serve`, or
`ABSTRACTGATEWAY_USER_AUTH=1`), the gateway creates `default/admin`, writes its
first token to `<data dir>/auth/bootstrap-admin-token`, and routes each user to
their own runtime and data plane (`1 user = 1 runtime`). Admins manage users
from the console or `/api/gateway/admin/users`; user tokens are returned once
and stored only as hashes. Browser apps exchange a user token for an opaque
session (`POST /api/gateway/session/login`, HTTP-only cookie plus CSRF token)
instead of keeping the token. `ABSTRACTGATEWAY_AUTH_TOKEN` is a shared
server/operator token, not a browser sign-in token. See
[docs/security.md](docs/security.md).

The built-in console at `/console` covers users and entities, runtimes,
workflows, provider connections, multimodal capability defaults, a sandbox,
host resources, models, engines, apps and network access. The same
configuration surfaces exist in a terminal through the `abstractgateway-console`
Rust app (`cargo install abstractgateway-console`). See
[docs/console.md](docs/console.md).

## Docker server

Release images are published to GHCR. The default image is the light,
portable server image:

```bash
docker pull ghcr.io/lpalbou/abstractgateway:0.4.2
```

NVIDIA hosts can try the experimental full GPU image when local
vLLM/HuggingFace/Diffusers engines are wanted. This image is published
best-effort until it has a real CUDA build and smoke gate:

```bash
docker pull ghcr.io/lpalbou/abstractgateway:0.4.2-gpu
```

The `abstractgateway-server` and `abstractgateway-server-nvidia` GHCR names are
published as aliases for existing deployments; use `abstractgateway` for new
ones.

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
- You do **not** need the `abstractflow` Python package to run `.flow` bundles. You only need it to author bundles; the gateway runs bundles only (store VisualFlows through `/api/gateway/visualflows/*` and publish them as bundles).

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

## Capability scope

Direct, run-scoped Gateway routes (each creates a durable child run and
returns artifacts):

- voice and audio: `POST /api/gateway/runs/{run_id}/voice/tts`,
  `POST /api/gateway/runs/{run_id}/audio/transcribe`
- images: `POST /api/gateway/runs/{run_id}/images/generate`, `/images/edit`,
  `/images/upscale`
- video: `POST /api/gateway/runs/{run_id}/videos/generate`,
  `/videos/from_image`
- music: `POST /api/gateway/runs/{run_id}/music/generate`
- run data: `GET /api/gateway/runs/{run_id}/input_data`,
  `GET /api/gateway/runs/{run_id}/history_bundle`

Discovery and catalogs for thin clients:

- `GET /api/gateway/discovery/capabilities`: a versioned contract of packages,
  plugins, endpoints and feature gates, with `common.readiness`
- voice, speech, transcription, music and vision catalogs
  (`/api/gateway/voice/voices`, `/audio/*/models`, `/audio/music/providers`,
  `/vision/*`), each with a `gateway_catalog_v1` envelope and canonical `items`
- `/api/gateway/artifacts/search`: cross-run, session and run artifact search
- `/api/gateway/kg/query`: KG memory queries (LanceDB by default)
- `/api/gateway/prompt_cache/*` and `/api/gateway/sessions/{session_id}/prompt_cache/*`:
  provider-dependent prompt-cache controls

Media generation needs a configured backend for the route (the console's
**Multimodal** tab). Image and video routes stream `abstract.progress` records
on the child run's ledger; prompt-cache support depends on the provider and
model. Details: [docs/api.md](docs/api.md) and [docs/faq.md](docs/faq.md).

## Client contract (replay-first)

- Clients **start runs**: `POST /api/gateway/runs/start`
  - optional `thinking` sets the run-scoped `_runtime.thinking` default used by
    Flow LLM/Agent nodes and AbstractAgent adapters when Core/provider support
    reasoning controls
- Clients can **schedule runs** (bundle mode): `POST /api/gateway/runs/schedule`
- Clients **act** by submitting durable commands: `POST /api/gateway/commands`
  - supported types: `pause|resume|cancel|emit_event|update_schedule|compact_memory`
- Clients **render** by replaying/streaming the durable ledger:
  - replay: `GET /api/gateway/runs/{run_id}/ledger?after=...`
  - stream (SSE): `GET /api/gateway/runs/{run_id}/ledger/stream?after=...`

Model residency is available from a shell through
`abstractgateway models loaded|load|unload` ([docs/console.md](docs/console.md)).

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
- `abstractgateway[tray]`: a menu bar / system tray icon for `abstractgateway serve` (macOS, Windows, Linux) — open the console, pause/resume workflows, unload models, watch memory and GPU, restart or update; see [docs/tray.md](docs/tray.md)
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

For details on capability route defaults, store backends, and workflow sources, see [docs/configuration.md](docs/configuration.md).

## Creating a `.flow` bundle (authoring)

Use AbstractFlow to pack a bundle:

```bash
abstractflow bundle pack /path/to/root.json --out /path/to/bundles/my.flow --flows-dir /path/to/flows
```

See [docs/getting-started.md](docs/getting-started.md) for running, split API/runner, and file→SQLite migration.

## Docs

Published docs site: https://www.lpalbou.info/AbstractGateway/

- Docs index: [docs/README.md](docs/README.md)
- First run: [docs/first-run.md](docs/first-run.md)
- Getting started: [docs/getting-started.md](docs/getting-started.md)
- Architecture: [docs/architecture.md](docs/architecture.md)
- API overview: [docs/api.md](docs/api.md)
- Configuration: [docs/configuration.md](docs/configuration.md)
- Consoles: [docs/console.md](docs/console.md)
- Apps: [docs/apps.md](docs/apps.md)
- Local engines: [docs/engines.md](docs/engines.md)
- Model downloads: [docs/model-downloads.md](docs/model-downloads.md)
- Desktop tray: [docs/tray.md](docs/tray.md)
- Security: [docs/security.md](docs/security.md)
- Deployment: [docs/deployment.md](docs/deployment.md)
- Shipped workflows: [docs/shipped-workflows.md](docs/shipped-workflows.md)
- FAQ: [docs/faq.md](docs/faq.md)
- Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md)
- Operator tooling: [docs/maintenance.md](docs/maintenance.md)

Project: [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) ·
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [SECURITY.md](SECURITY.md) ·
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) · [LICENSE](LICENSE) (MIT)
