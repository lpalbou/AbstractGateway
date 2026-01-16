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

### Default (bundle mode)

```bash
pip install abstractgateway
```

Bundle mode executes **WorkflowBundles** (`.flow`) via **WorkflowArtifacts** without importing `abstractflow`.

### Optional (compatibility): VisualFlow JSON

```bash
pip install "abstractgateway[visualflow]"
```

This mode depends on the **AbstractFlow compiler library** (`abstractflow`) to interpret VisualFlow JSON (it does **not** require the AbstractFlow web UI/app).

## Run

```bash
export ABSTRACTGATEWAY_DATA_DIR="./runtime"
export ABSTRACTGATEWAY_FLOWS_DIR="/path/to/bundles-or-flow"

# Security (recommended)
export ABSTRACTGATEWAY_AUTH_TOKEN="your-token"
# Allow your browser app Origin(s). Wildcard ports are supported (dev convenience).
# Example (LAN dev): "http://localhost:*,http://127.0.0.1:*,http://192.168.1.188:*"
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="http://localhost:*,http://127.0.0.1:*"

abstractgateway serve --host 127.0.0.1 --port 8080
```

Notes:
- `ABSTRACTGATEWAY_WORKFLOW_SOURCE` defaults to `bundle`. Valid values:
  - `bundle` (default): `ABSTRACTGATEWAY_FLOWS_DIR` points to a directory containing `*.flow` bundles (or a single `.flow` file)
  - `visualflow` (compat): `ABSTRACTGATEWAY_FLOWS_DIR` points to a directory containing `*.json` VisualFlow files
- For production, run behind HTTPS (reverse proxy) and set exact allowed origins.

## Creating a `.flow` bundle (authoring)

Use AbstractFlow to pack a bundle:

```bash
abstractflow bundle pack /path/to/root.json --out /path/to/bundles/my.flow --flows-dir /path/to/flows
```

## Starting a run (bundle mode)

If the bundle has a **single entrypoint** (or declares `manifest.default_entrypoint`), you can start it with just `bundle_id` + `input_data`:

```bash
curl -sS -X POST "http://localhost:8080/api/gateway/runs/start" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{"bundle_id":"my-bundle","input_data":{"prompt":"Hello"}}'
```

If the bundle exposes **multiple entrypoints** and has no default, you must select one with `flow_id`:

```bash
curl -sS -X POST "http://localhost:8080/api/gateway/runs/start" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{"bundle_id":"my-bundle","flow_id":"ac-echo","input_data":{"prompt":"Hello"}}'
```

For backwards-compatible clients, you can also pass a namespaced id as `flow_id` (`"my-bundle:ac-echo"`) without sending `bundle_id`.

## Docs
- Architecture: `docs/architecture.md` (framework) and `abstractgateway/docs/architecture.md` (this package)
- Deployment: `docs/guide/deployment.md`
