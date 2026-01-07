# AbstractGateway

AbstractGateway is the **deployable Run Gateway host** for AbstractRuntime runs:
- durable command inbox
- ledger replay/stream
- security baseline (token + origin + limits)

This decouples the gateway service from any specific UI (AbstractFlow, AbstractCode, web/PWA thin clients).

## What it does (contract)
- Clients **act** by submitting durable commands: `start`, `resume`, `pause`, `cancel`, `emit_event`
- Clients **render** by replaying/streaming the durable ledger (cursor-based, replay-first)

Endpoints:
- `POST /api/gateway/runs/start`
- `GET /api/gateway/runs/{run_id}`
- `GET /api/gateway/runs/{run_id}/ledger`
- `GET /api/gateway/runs/{run_id}/ledger/stream` (SSE)
- `POST /api/gateway/commands`

## Install

For VisualFlow JSON workflows (current flows format):

```bash
pip install "abstractgateway[visualflow]"
```

Note:
- `abstractgateway[visualflow]` depends on the **AbstractFlow compiler library** (`abstractflow`) to interpret VisualFlow JSON.
- It does **not** require the AbstractFlow web UI/app.

## Run

```bash
export ABSTRACTGATEWAY_DATA_DIR="./runtime"
export ABSTRACTGATEWAY_FLOWS_DIR="/path/to/flows"

# Security (recommended)
export ABSTRACTGATEWAY_AUTH_TOKEN="your-token"
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="*"

abstractgateway serve --host 127.0.0.1 --port 8080
```

Notes:
- `ABSTRACTGATEWAY_FLOWS_DIR` is **any directory** that contains `*.json` VisualFlow workflow files (each file contains `"id": "<flow_id>"`).
- For production, run behind HTTPS (reverse proxy) and set exact allowed origins.

## Docs
- Architecture: `docs/architecture.md` (framework) and `abstractgateway/docs/architecture.md` (this package)
- Deployment: `docs/guide/deployment.md`


