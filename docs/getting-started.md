# AbstractGateway — Getting started

AbstractGateway is a deployable HTTP/SSE host for **durable AbstractRuntime runs**:
- clients **start runs** and submit **durable commands**
- clients **render** by replaying/streaming the durable ledger (replay-first)

This guide gets a new installation running in **bundle mode** (recommended), then covers **file vs SQLite** durability and a best-effort **file → SQLite** migration.

## AbstractFramework ecosystem (context)

AbstractGateway is one component in the larger **AbstractFramework** ecosystem:
- **AbstractRuntime** (required): durable runs + workflow registry + stores
- **AbstractCore / AbstractVoice / AbstractVision** (optional via `abstractgateway[multimodal]` / `[server]`): LLM/tool execution, provider-level prompt-cache controls, and workflow-backed/direct generated image/voice/audio capabilities used by many bundles

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

## Prerequisites

- Python `>=3.10` (see `pyproject.toml`)
- Workflow source:
  - **Bundle mode** (recommended): one `.flow` file or a directory of `*.flow` bundles
    - You can also upload bundles after startup via `POST /api/gateway/bundles/upload` (see below)
  - **VisualFlow directory mode** (compat): a directory of `*.json` VisualFlow files (requires `abstractgateway[visualflow]`)

## Install

```bash
# Core package (runner + stores + CLI)
pip install abstractgateway

# HTTP server (FastAPI + Uvicorn)
pip install "abstractgateway[http]"

# Turnkey server/container profile (HTTP + Runtime/Core multimodal + remote providers/tools/media)
pip install "abstractgateway[server]"

# KG memory support (AbstractMemory TripleStore API; LanceDB default, SQLite if available)
pip install "abstractgateway[memory]"

# Experimental NVIDIA full profile (vLLM/HuggingFace + local Diffusers + local voice engines)
pip install "abstractgateway[server-nvidia]"

# Explicit voice/audio profile (TTS + STT endpoints)
pip install "abstractgateway[voice]"

# Explicit generative vision profile through AbstractCore's AbstractVision plugin
pip install "abstractgateway[vision]"

# Or: batteries-included (HTTP + tools + voice/audio + vision + media + visualflow)
pip install "abstractgateway[all]"
```

Optional (only if your workflows need it):
- LLM/tool nodes (bundle mode): `pip install "abstractgateway[multimodal]"` or `pip install "abstractgateway[server]"`
- Runtime-managed generated images, generated voice, and STT inside workflows: `pip install "abstractgateway[multimodal]"` or `pip install "abstractgateway[server]"`
- Server deployments with hosted providers / OpenAI-compatible endpoints: `pip install "abstractgateway[server]"`
- Visual Agent nodes (bundle mode): `pip install abstractagent` (already included by `abstractgateway[server]`)
- Voice/audio endpoints (TTS/STT): `pip install "abstractgateway[voice]"` (or `pip install abstractvoice`)
- Generative vision: `pip install "abstractgateway[vision]"` (or `pip install abstractvision`)
- `memory_kg_*` nodes (bundle mode): `pip install "abstractgateway[memory]"`

## 1) Run (bundle mode, file-backed stores)

File-backed stores are the default and easiest for dev.

```bash
export ABSTRACTGATEWAY_WORKFLOW_SOURCE=bundle
export ABSTRACTGATEWAY_FLOWS_DIR="/path/to/bundles"      # directory with *.flow (or a single .flow file)
export ABSTRACTGATEWAY_DATA_DIR="$PWD/runtime/gateway"

# Required by default: the server refuses to start without a token.
export ABSTRACTGATEWAY_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="http://localhost:*,http://127.0.0.1:*"

abstractgateway serve --host 127.0.0.1 --port 8080
```

OpenAPI docs (Swagger UI): `http://127.0.0.1:8080/docs` (use **Authorize** for bearer token)

Smoke checks:

```bash
curl -sS "http://127.0.0.1:8080/api/health"

curl -sS -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  "http://127.0.0.1:8080/api/gateway/bundles"
```

If `bundles.items` is empty, either:
- point `ABSTRACTGATEWAY_FLOWS_DIR` at a directory containing `*.flow` files (or a single `.flow` file), or
- upload a bundle via the API:

```bash
curl -sS -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  -F "file=@./my-bundle@0.1.0.flow" \
  -F "overwrite=false" \
  -F "reload=true" \
  "http://127.0.0.1:8080/api/gateway/bundles/upload"
```

You can also create a local env file and inspect readiness with:

```bash
abstractgateway-config init --env-file .env
abstractgateway-config status
```

## 2) Start a run (bundle mode)

First, discover entrypoints from `GET /api/gateway/bundles`. Then start a run:

```bash
curl -sS -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"bundle_id":"my-bundle","input_data":{"prompt":"Hello"}}' \
  "http://127.0.0.1:8080/api/gateway/runs/start"
```

Notes:
- If a bundle has multiple entrypoints and no default, you must pass `flow_id`.
- See [api.md](./api.md) for ledger replay/stream and durable commands.

## 2b) (Optional) Schedule a run (bundle mode)

To launch a workflow periodically, start a **scheduled parent run**:

```bash
curl -sS -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"bundle_id":"my-bundle","flow_id":"ac-echo","input_data":{"prompt":"Ping"},"start_at":"now","interval":"1h","repeat_count":3}' \
  "http://127.0.0.1:8080/api/gateway/runs/schedule"
```

Tip: to stop a schedule, cancel the scheduled parent run (`POST /api/gateway/commands`, type `cancel`).

## 3) Split API vs runner (recommended for upgrades)

By default, `abstractgateway serve` starts the HTTP API **and** the runner loop in the same process.

To restart the HTTP API without pausing durable execution, run two processes sharing the same `ABSTRACTGATEWAY_DATA_DIR`:

```bash
# Process 1 (runner worker, no HTTP deps needed):
abstractgateway runner

# Process 2 (HTTP API only):
abstractgateway serve --no-runner --host 127.0.0.1 --port 8080
```

## 3b) Docker / Compose

For a containerized deployment with AbstractRuntime multimodal support,
AbstractCore remote providers, workflow-backed/direct AbstractVision image
generation, direct Gateway voice/audio/image endpoints, and provider/session
prompt-cache controls included:

```bash
export ABSTRACTGATEWAY_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

docker run --rm -p 127.0.0.1:8080:8080 \
  -e ABSTRACTGATEWAY_AUTH_TOKEN="$ABSTRACTGATEWAY_AUTH_TOKEN" \
  -v "$PWD/runtime/gateway:/data/gateway" \
  -v "$PWD/flows/bundles:/data/flows:ro" \
  ghcr.io/lpalbou/abstractgateway-server:0.2.4
```

See [deployment.md](./deployment.md) for Compose, provider keys, and image
customization.

NVIDIA hosts can try `ghcr.io/lpalbou/abstractgateway-server-nvidia:0.2.4`
with the compose overlay in `docker/abstractgateway-server/compose.nvidia.yml`.
It is experimental until a real CUDA build/smoke gate is part of release
validation.
Apple MLX inference should run natively on macOS rather than in Docker because
Linux containers do not get access to Apple's Metal/MLX runtime. The container
can still use native macOS inference through an OpenAI-compatible endpoint:
point `OPENAI_COMPATIBLE_BASE_URL` at Docker Model Runner on
`http://model-runner.docker.internal/engines/v1`, LM Studio on
`http://host.docker.internal:1234/v1`, `mlx_lm.server`, or Ollama's
OpenAI-compatible API on `http://host.docker.internal:11434/v1` when the
native Ollama model path uses MLX. For native non-Docker installs, use
`pip install "abstractgateway[apple]"` or `pip install "abstractgateway[all-apple]"`
on Apple Silicon, and `pip install "abstractgateway[gpu]"` or
`pip install "abstractgateway[all-gpu]"` on GPU workstations.

## 4) What’s stored in `ABSTRACTGATEWAY_DATA_DIR` (file backend)

When `ABSTRACTGATEWAY_STORE_BACKEND=file` (default), the gateway persists (via `abstractruntime` stores):
- `run_<run_id>.json` (checkpointed run state)
- `ledger_<run_id>.jsonl` (append-only step records)
- `commands.jsonl` and `commands_cursor.json` (durable inbox + runner cursor)
- `artifacts/` (offloaded blobs/attachments)
- `dynamic_flows/` (gateway-generated wrapper flows, e.g. schedules)
- `workspaces/` (per-run workspaces created at run start when `workspace_root` is not provided)

## 5) Enable SQLite-backed stores

SQLite-backed stores eliminate directory scanning and move run/ledger/inbox data into indexed tables.

Artifacts remain file-backed under `ABSTRACTGATEWAY_DATA_DIR/artifacts/`.

```bash
export ABSTRACTGATEWAY_STORE_BACKEND=sqlite

# Optional; when omitted, defaults to: <ABSTRACTGATEWAY_DATA_DIR>/gateway.sqlite3
export ABSTRACTGATEWAY_DB_PATH="$PWD/runtime/gateway/gateway.sqlite3"
#
# Safety invariant: when using sqlite, the DB file must live under ABSTRACTGATEWAY_DATA_DIR.
# The gateway will refuse to start if ABSTRACTGATEWAY_DB_PATH points outside (prevents UAT/prod cross-wiring).

abstractgateway serve --host 127.0.0.1 --port 8080
```

## 6) Migrate an existing file-backed data dir → SQLite

This is a **best-effort** local migration (`abstractgateway migrate`) that reads:
- `run_*.json`
- `ledger_*.jsonl`
- `commands.jsonl`
- `commands_cursor.json`

and writes a single SQLite DB file. It does **not** delete the original files.

```bash
cp -a runtime/gateway "runtime/gateway.file-backup.$(date +%Y%m%d-%H%M%S)"

abstractgateway migrate --from=file --to=sqlite \
  --data-dir runtime/gateway \
  --db-path runtime/gateway/gateway.sqlite3
```

## Related docs

- Docs index: [README.md](./README.md)
- FAQ: [faq.md](./faq.md)
- Architecture: [architecture.md](./architecture.md)
- Configuration (env vars + optional deps): [configuration.md](./configuration.md)
- Deployment: [deployment.md](./deployment.md)
- API overview: [api.md](./api.md)
- Security: [security.md](./security.md)
- Operator tooling (optional): [maintenance.md](./maintenance.md)
