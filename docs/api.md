# AbstractGateway — API overview

The HTTP API is implemented with FastAPI under the `/api` prefix:
- Health: `GET /api/health`
- Gateway surface: `/api/gateway/*` (durable runs + operator tooling)

The API is documented at runtime:
- OpenAPI JSON: `GET /openapi.json`
- Swagger UI: `GET /docs` (use **Authorize** to paste the bearer token)

Context:
- In the AbstractFramework ecosystem, UIs and automations call this API to operate **AbstractRuntime** runs.
- Architecture diagram and core concepts: [architecture.md](./architecture.md)

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

### 2b) Schedule a run (bundle mode)

`POST /api/gateway/runs/schedule` starts a **scheduled parent run** that launches the target workflow as child runs over time.

Example (run 3 times, every hour, starting now):

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"bundle_id":"my-bundle","flow_id":"ac-echo","input_data":{"prompt":"Ping"},"start_at":"now","interval":"1h","repeat_count":3,"share_context":true,"session_id":"sess-1"}' \
  "$BASE_URL/api/gateway/runs/schedule"
```

Notes:
- `start_at`: ISO 8601 timestamp (recommended) or `"now"`.
- `interval`: e.g. `"15m"`, `"1h"`, `"2d"`. If omitted, runs once.
- `repeat_count`: if omitted and `interval` is set, repeats forever. Alternatively use `repeat_until` (ISO 8601).
- To stop a schedule, cancel the scheduled parent run via `POST /api/gateway/commands` with type `cancel`.

Evidence: `ScheduleRunRequest`, `start_scheduled_run` in `src/abstractgateway/routes/gateway.py`.

### 3) Replay the ledger (cursor-based)

Ledger pages are replayed using `after` as “number of items already consumed”.

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/runs/<run_id>/ledger?after=0&limit=200"
```

Response shape:
- `items`: list of durable ledger records
- `next_after`: the next cursor to use

Evidence: `src/abstractgateway/routes/gateway.py` (`get_ledger`).

### 3b) Replay ledgers for multiple runs (batch)

Use `POST /api/gateway/runs/ledger/batch` to reduce request fanout when observing many runs/subflows.

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"limit":200,"runs":[{"run_id":"<run_id_1>","after":0},{"run_id":"<run_id_2>","after":0}]}' \
  "$BASE_URL/api/gateway/runs/ledger/batch"
```

Evidence: `src/abstractgateway/routes/gateway.py` (`get_ledger_batch`).

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

## Discovery endpoints (optional)

These exist to help thin clients adapt to the deployed gateway.

- Capabilities (best-effort): `GET /api/gateway/discovery/capabilities`
- Providers/models discovery (best-effort): `GET /api/gateway/discovery/providers`, `GET /api/gateway/discovery/providers/{provider}/models`
- Dynamic capability catalogs: `GET /api/gateway/voice/voices`, `GET /api/gateway/audio/speech/models`, `GET /api/gateway/audio/transcriptions/models`, `GET /api/gateway/audio/music/providers`, `GET /api/gateway/audio/music/models`, `GET /api/gateway/vision/provider_models`

The capabilities payload includes package presence (`abstractruntime`,
`abstractcore`, `abstractmemory`, `abstractvoice`, `abstractvision`), existing
gateway helpers (`tools`, `visualflow`, `media`), memory-store readiness, and
AbstractCore capability plugin status for `voice`, `audio`, `vision`, and
`music`.

Today the route paths and contract descriptors are the stable part of this
surface. Catalog routes now also include a stable Gateway-owned envelope:

- `catalog.contract = gateway_catalog_v1`
- `catalog.version = 1`
- `items = [...]`

Legacy lower-layer fields are still preserved for compatibility. New thin
clients should read `catalog` plus `items`; older clients can keep using route-
specific fields like `models`, `provider_models`, `profiles`, or `voices`.

Provider discovery also reports the resolved default provider/model when one is
configured. The resolver follows request values, Gateway env, and AbstractCore
global defaults; if no pair exists, the response includes `default_error` rather
than a hardcoded local model.

It also includes a versioned thin-client contract:

- `capabilities.contracts.version`: currently `1`
- `capabilities.contracts.common`: shared run start/list/summary/input/history,
  ledger, artifact, attachment, workspace, discovery, and provider prompt-cache
  controls
- `capabilities.contracts.common.readiness`: compact Gateway-owned
  `gateway_surface_readiness_v1` summary derived from the shared endpoint/media/
  residency descriptors
- `capabilities.contracts.flow_editor`: the AbstractFlow editor/runtime surface
- `capabilities.contracts.assistant`: assistant-facing voice/audio/media/cache
  feature gates
- `capabilities.contracts.abstractcode`: code-client run/history/workspace/cache
  feature gates

Contract booleans are intentionally conservative. Package `installed=true` is
not the same thing as endpoint `available=true`; clients should branch on the
versioned contract fields when enabling controls.

`common.readiness` is intentionally narrower than provider/backend health. It
summarizes Gateway surface availability from existing descriptors, but it does
not invent selected backend/provider/model truth or stable degraded-state
reason codes.

Evidence: `src/abstractgateway/routes/gateway.py` (`discovery_capabilities`, `discovery_providers`).

## AbstractFlow gateway-first editor contract

The browser editor can use AbstractGateway as its runtime and storage host.

Draft VisualFlow records:

- `GET /api/gateway/visualflows`
- `POST /api/gateway/visualflows`
- `GET /api/gateway/visualflows/{flow_id}`
- `PUT /api/gateway/visualflows/{flow_id}`
- `DELETE /api/gateway/visualflows/{flow_id}`
- `POST /api/gateway/visualflows/{flow_id}/publish`

Bundle inspection and editor run-schema helpers:

- `GET /api/gateway/bundles`
- `GET /api/gateway/bundles/{bundle_id}`
- `GET /api/gateway/bundles/{bundle_id}/flows/{flow_id}`
- `GET /api/gateway/bundles/{bundle_id}/flows/{flow_id}/input_schema`

The input-schema endpoint returns a versioned payload with:

- `version`
- `bundle_id`, `bundle_version`, `bundle_ref`, `flow_id`, `workflow_id`
- `inputs`: entrypoint input pins derived from the `on_flow_start` node
- `defaults`: pin defaults from VisualFlow JSON
- `input_data_schema`: a small JSON Schema object for the Run Flow modal

Example:

```bash
curl -sS -H "$AUTH" \
  "$BASE_URL/api/gateway/bundles/my-bundle/flows/ac-echo/input_schema"
```

The editor observes runs with the core lifecycle endpoints above:
`/runs/start`, `/runs/{run_id}`, `/runs/{run_id}/ledger`,
`/runs/{run_id}/ledger/stream`, `/runs/ledger/batch`,
`/runs/{run_id}/input_data`, `/runs/{run_id}/history_bundle`, and
`/runs/{run_id}/artifacts`.

## Optional multimodal scope

Current direct Gateway endpoints:
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

The catalog endpoints proxy AbstractCore Server routes when
`ABSTRACTCORE_SERVER_BASE_URL`
is configured. Gateway uses explicit Core auth settings for that hop and never
reuses the Gateway bearer token as a Core/provider secret. Without a configured
Core server, the voice/model routes return bounded static descriptors from
Gateway and capability-package environment variables.

Each route now adds:

- `catalog`: Gateway-owned route metadata (`contract`, `version`, `kind`,
  `scope`, `route_source`, optional `upstream_source`, and route filters)
- `items`: one canonical primary array for thin clients

Examples:

- `/voice/voices`: `items` contain voice/profile records with `id`, `label`,
  optional `provider`, optional `model`, and `voice_kind`
- `/audio/*/models`: `items` contain model records with `id`, `label`,
  optional `provider`, optional `tasks`, and optional `parameters`
- `/audio/music/providers` and `/discovery/providers`: `items` contain provider
  records with `id`, `label`, and `provider`

Generated images are available through Runtime workflows when a compatible
image backend is installed and configured. Gateway also exposes a direct image
generation endpoint that uses the Runtime/Core output-selector contract rather
than a provider-specific image client. The route stores the generated image as a
run artifact and emits `abstract.media.image.generated` with:

- `run_id`, `request_id`, `prompt`
- optional `provider`, `model`, `size`, `width`, `height`, and `format`
- `image_artifact`: `{"$artifact", "content_type", "filename", "sha256", "size_bytes"}`

If the active workflow runtime already has an AbstractCore LLM client, the route
uses it. For tools-only workflows, the route can create a direct Runtime/Core
client from request `provider`/`model` or `ABSTRACTGATEWAY_PROVIDER` /
`ABSTRACTGATEWAY_MODEL`. Unsupported or unconfigured deployments return a
structured `ok=false` response instead of a failed run.

Gateway also exposes a direct image-edit sibling route:

- `POST /api/gateway/runs/{run_id}/images/edit`

The request uses a source `image_artifact`, optional `mask_artifact`, the same
provider/model and image backend selectors as image generation, and returns an
artifact-backed edited image. Thin clients should feature-detect it from
`capabilities.contracts.flow_editor.media.edited_image` or
`capabilities.contracts.assistant.media.edited_image`.

Generated music follows the same direct child-run pattern. Thin clients should
discover it from `capabilities.contracts.flow_editor.media.generated_music` or
`capabilities.contracts.assistant.media.generated_music`, list providers/models
from the music catalog routes, and treat the returned `child_run_id` plus
`music_artifact` as the durable output handle.

STT and listen contract notes:

- `POST /api/gateway/runs/{run_id}/audio/transcribe` accepts a run-visible
  `audio_artifact` plus optional `language`, `prompt`, `response_format`,
  `temperature`, `format`, `provider`, and `model` hints.
- `capabilities.contracts.flow_editor.voice.stt` and
  `capabilities.contracts.assistant.voice.stt` point to that upload route.
- `capabilities.contracts.flow_editor.voice.listen` and
  `capabilities.contracts.assistant.voice.listen` are host-capture contracts,
  not a live microphone socket. They tell higher apps to capture locally and
  emit an event or upload the resulting audio artifact.

## KG memory

`POST /api/gateway/kg/query` queries the configured AbstractMemory TripleStore.
Gateway resolves the store through:

- `ABSTRACTGATEWAY_MEMORY_STORE_BACKEND=lancedb|memory` (`sqlite` when the installed AbstractMemory build exposes `SQLiteTripleStore`)
- `ABSTRACTGATEWAY_MEMORY_STORE_PATH`
- `ABSTRACTGATEWAY_MEMORY_REQUIRE_VECTOR`

Structured queries work with LanceDB and in-memory stores. SQLite also works
when the installed AbstractMemory build exposes `SQLiteTripleStore`. Semantic
`query_text` requires a vector-capable backend plus configured Gateway
embeddings; SQLite returns a clear 400 instead of pretending to support semantic
recall.

## Prompt-cache control plane (operator API)

The gateway exposes prompt-cache operator endpoints under `/api/gateway/prompt_cache/*`.

Core endpoints:

- `GET /api/gateway/prompt_cache/capabilities?provider=...&model=...`
- `GET /api/gateway/prompt_cache/stats?provider=...&model=...`
- `POST /api/gateway/prompt_cache/set`
- `POST /api/gateway/prompt_cache/update`
- `POST /api/gateway/prompt_cache/fork`
- `POST /api/gateway/prompt_cache/clear`
- `POST /api/gateway/prompt_cache/prepare_modules`

Behavior:

- These routes use the runtime's AbstractCore prompt-cache client contract rather than directly depending on provider-instance access.
- In local mode they delegate to the in-process provider.
- In remote/hybrid mode they follow whatever `/acore/prompt_cache/*` surface the configured AbstractCore server exposes.
- All core prompt-cache responses include `operation` and `capabilities`, with structured unsupported/error cases (`code="prompt_cache_unsupported"` / `code="prompt_cache_error"` / `code="prompt_cache_unavailable"`).
- These endpoints remain provider/model controls, not a Gateway-owned CachedSession persistence system.

Session lifecycle endpoints:

- `GET /api/gateway/sessions/{session_id}/prompt_cache/status`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/prepare`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/rebuild`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/clear`

These routes derive a deterministic bounded namespace/key from `session_id`,
`bundle_id`, `bundle_version`, `flow_id`, `provider`, `model`, optional
`template_id`, and `version`. They expose three honest modes:

- `unsupported`: provider/model does not expose prompt-cache support; responses include `supported=false`, `ok=false`, and capabilities.
- `keyed`: gateway returns a stable `runtime_hint`/`prompt_cache_key` for Runtime/Core injection, but does not claim module preparation occurred.
- `local_control_plane`: gateway uses supported provider operations such as `prepare_modules`, `fork`, `set`, `clear`, and `stats`.

`status` is read-only. `prepare` accepts optional modules (`system_prompt`,
`workflow_instructions`, `tools`, `pinned_attachments`) and returns either
provider operation results or a key hint. `rebuild` is clear-plus-prepare for
providers that expose clear controls.

Durable bloc exact-reuse endpoints:

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

These routes are the primary app-facing durable prompt-cache path:

- create or identify a durable text bloc;
- ensure or load a KV artifact for a target local provider/model;
- use the returned `prompt_cache_binding` in later Runtime-backed generation;
- list/delete/prune artifacts without reaching into provider-private cache state.

They delegate through Runtime's public AbstractCore host facade rather than
proxying Core directly. They are operator-style host controls, so the routes
themselves are not ledgered run execution; the ledgered exact-reuse path is the
later `LLM_CALL.params.prompt_cache_binding` used inside real Runtime runs.

Host-local prompt-cache export/import admin aliases:

- `GET /api/gateway/prompt_cache/saved`
- `POST /api/gateway/prompt_cache/save`
- `POST /api/gateway/prompt_cache/load`

These remain explicitly local/operator-oriented:

- the route paths are compatibility aliases, but the implementation delegates to Runtime's public host facade:
  - `saved` -> `list_prompt_cache_exports(...)`
  - `save` -> `prompt_cache_export(...)`
  - `load` -> `prompt_cache_import(...)`
- local bundle/file runtimes store these exports under the Gateway data dir at `prompt_cache_exports/`
- remote and hybrid runtimes return `code=prompt_cache_local_only`
- response payloads follow Runtime's host-local export/import contract, including `operation`, `local_only`, `artifact_*`, `capabilities`, and `provider_response`

## Email inbox (operator UI; optional)

These endpoints power AbstractObserver’s **Inbox → Email** UI. They are **account-scoped**: the browser cannot supply arbitrary IMAP/SMTP host/user credentials. The gateway host must be configured with one or more email accounts (multi-account YAML or env vars).

Endpoints:
- `GET /api/gateway/email/accounts`
- `GET /api/gateway/email/messages?account=…&mailbox=…&since=…&status=…&limit=…`
- `GET /api/gateway/email/messages/{uid}?account=…&mailbox=…&max_body_chars=…`
- `POST /api/gateway/email/send`

Examples:

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/email/accounts"
```

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/email/messages?status=unread&since=7d&limit=20"
```

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/email/messages/12345?max_body_chars=20000"
```

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"to":"you@example.com","subject":"Hello","body_text":"Hi!"}' \
  "$BASE_URL/api/gateway/email/send"
```

Configuration notes (gateway host):
- Multi-account: set `ABSTRACT_EMAIL_ACCOUNTS_CONFIG=/path/to/emails.yaml` (recommended).
- Single-account env fallback: set `ABSTRACT_EMAIL_IMAP_*` and/or `ABSTRACT_EMAIL_SMTP_*`.
- The secret itself must be present in the env var referenced by `*_PASSWORD_ENV_VAR` (e.g. `EMAIL_PASSWORD=...`).

Evidence: `src/abstractgateway/routes/gateway.py` (`/email/accounts|messages|send`) which proxies to the Runtime AbstractCore comms facade.

Troubleshooting and common questions: [faq.md](./faq.md).
