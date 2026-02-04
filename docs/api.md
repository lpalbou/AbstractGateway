# AbstractGateway — API overview

The HTTP API is implemented with FastAPI under the `/api` prefix:
- Health: `GET /api/health`
- Gateway surface: `/api/gateway/*` (durable runs + operator tooling)

The API is documented at runtime:
- OpenAPI JSON: `GET /openapi.json`
- Swagger UI: `GET /docs`

## Auth

By default, `/api/gateway/*` is protected by `GatewaySecurityMiddleware` (bearer token + origin allowlist).  
See: [security.md](./security.md).

All examples below assume:

```bash
export BASE_URL="http://127.0.0.1:8080"
export AUTH="Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN"
```

## Core workflow lifecycle

### 1) List bundles (bundle mode)

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/bundles"
```

Upload a bundle:

```bash
curl -sS -H "$AUTH" \
  -F "file=@./my-bundle@0.1.0.flow" \
  -F "overwrite=false" \
  -F "reload=true" \
  "$BASE_URL/api/gateway/bundles/upload"
```

### 2) Start a run

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"bundle_id":"my-bundle","input_data":{"prompt":"Hello"}}' \
  "$BASE_URL/api/gateway/runs/start"
```

If you need a specific entrypoint:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"bundle_id":"my-bundle","flow_id":"ac-echo","input_data":{"prompt":"Hello"}}' \
  "$BASE_URL/api/gateway/runs/start"
```

Evidence: request/response models live in `src/abstractgateway/routes/gateway.py` (`StartRunRequest`, `start_run`).

### 3) Replay the ledger (cursor-based)

Ledger pages are replayed using `after` as “number of items already consumed”.

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/runs/<run_id>/ledger?after=0&limit=200"
```

Response shape:
- `items`: list of durable ledger records
- `next_after`: the next cursor to use

Evidence: `src/abstractgateway/routes/gateway.py` (`get_ledger`).

### 4) Stream ledger updates (SSE)

SSE is an optimization; clients should always be able to reconnect by replaying from the last `next_after`.

```bash
curl -N -H "$AUTH" "$BASE_URL/api/gateway/runs/<run_id>/ledger/stream?after=0"
```

Evidence: `src/abstractgateway/routes/gateway.py` (`stream_ledger`).

## Durable commands (`POST /api/gateway/commands`)

Commands are appended to a durable inbox and applied asynchronously by the runner.

Request fields (see `SubmitCommandRequest` in `src/abstractgateway/routes/gateway.py`):
- `command_id`: client-supplied idempotency key (UUID recommended)
- `run_id`: target run id (or session id for some event use-cases)
- `type`: `pause|resume|cancel|emit_event|update_schedule|compact_memory`
- `payload`: command-specific object

### Pause / cancel

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"command_id":"'"$(python -c 'import uuid; print(uuid.uuid4())')"'", "run_id":"<run_id>", "type":"pause", "payload":{"reason":"operator_pause"}}' \
  "$BASE_URL/api/gateway/commands"
```

### Resume a paused run

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"command_id":"'"$(python -c 'import uuid; print(uuid.uuid4())')"'", "run_id":"<run_id>", "type":"resume", "payload":{}}' \
  "$BASE_URL/api/gateway/commands"
```

### Resume a WAITING run with a payload (WAIT resume)

When `payload.payload` is present, the runner interprets this as “resume a WAITING run with a durable payload”:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"command_id":"'"$(python -c 'import uuid; print(uuid.uuid4())')"'", "run_id":"<run_id>", "type":"resume", "payload":{"wait_key":"<optional_wait_key>", "payload":{"approved":true}}}' \
  "$BASE_URL/api/gateway/commands"
```

Evidence: `src/abstractgateway/runner.py` (`_apply_command`, `_apply_run_control`).

### Emit an external event

Minimal form:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"command_id":"'"$(python -c 'import uuid; print(uuid.uuid4())')"'", "run_id":"<session_id>", "type":"emit_event", "payload":{"name":"chat.message","payload":{"text":"hi"}}}' \
  "$BASE_URL/api/gateway/commands"
```

Evidence: `src/abstractgateway/runner.py` (`_apply_emit_event`).

## Beyond the core

`/api/gateway/*` also includes optional operator/tooling endpoints (reports inbox, triage queue, backlog browsing + exec runner, process manager, file/attachment helpers, embeddings, voice, discovery, …).  
See: [maintenance.md](./maintenance.md).

Troubleshooting and common questions: [faq.md](./faq.md).
