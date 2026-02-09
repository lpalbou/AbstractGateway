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
- **AbstractCore** (optional, via `abstractruntime[abstractcore]`): LLM/tool execution wiring used by many bundles (`src/abstractgateway/hosts/bundle_host.py`)
- Higher-level UIs (optional): AbstractFlow (authoring/bundling), AbstractCode / AbstractObserver / thin clients (rendering + operations)

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

## Quickstart (HTTP server, bundle mode)

```bash
pip install "abstractgateway[http]"

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

### Base (runner + stores + CLI)

Requires Python `>=3.10` (see `pyproject.toml`).

```bash
pip install abstractgateway
```

### HTTP API/SSE server (FastAPI + Uvicorn)

```bash
pip install "abstractgateway[http]"
```

### Optional extras

- `abstractgateway[visualflow]`: run VisualFlow JSON from a directory of `*.json` files (requires `abstractflow`)
- `abstractgateway[telegram]`: Telegram bridge dependencies
- `abstractgateway[voice]`: enable voice/audio endpoints (TTS/STT) via `abstractvoice`
- `abstractgateway[all]`: batteries-included install (HTTP + tools + voice + media + visualflow)
- `abstractgateway[dev]`: local test/dev deps

### Bundle-dependent dependencies (only if your workflows need them)

- LLM/tool nodes in bundle mode require AbstractRuntime’s AbstractCore integration.
  - If you installed `abstractgateway[http]`, this is already included.
  - If you installed only the base package, install it explicitly:

```bash
pip install "abstractruntime[abstractcore]>=0.4.2"
```

Visual Agent nodes require `abstractagent` (also included by `abstractgateway[http]`):

```bash
pip install abstractagent
```

For details on `ABSTRACTGATEWAY_PROVIDER`/`MODEL`, store backends, and workflow sources, see [docs/configuration.md](docs/configuration.md).

## Creating a `.flow` bundle (authoring)

Use AbstractFlow to pack a bundle:

```bash
abstractflow bundle pack /path/to/root.json --out /path/to/bundles/my.flow --flows-dir /path/to/flows
```

See [docs/getting-started.md](docs/getting-started.md) for running, split API/runner, and file→SQLite migration.

## Docs

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
- API overview: [docs/api.md](docs/api.md)
- Security: [docs/security.md](docs/security.md)
- Operator tooling (optional): [docs/maintenance.md](docs/maintenance.md)
