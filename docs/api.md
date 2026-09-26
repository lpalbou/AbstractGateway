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

## Route families

This page covers the run contract, artifacts, discovery, media, models and
host state. Other route families are documented next to the feature they
serve:

| Routes | Purpose | Reference |
|---|---|---|
| `/api/gateway/session/login`, `/session/logout`, `/session/claim`, `/me` | browser sessions, one-time sign-in links, the current principal | [security.md](./security.md), [first-run.md](./first-run.md) |
| `/api/gateway/admin/users`, `/admin/runtime-reservations` | user accounts and retained runtimes (admin) | [security.md](./security.md#tenant-and-user-isolation) |
| `/api/gateway/admin/runtime-config` | runtime settings (admin) | [configuration.md](./configuration.md) |
| `/api/gateway/network`, `/network/restart` | network exposure, addresses, reverse proxy | [configuration.md](./configuration.md#api-gateway_network_v1) |
| `/api/gateway/apps/*`, `/apps/handover/{code}`, `/apps/tui-handover` | browser apps, terminal apps, the Assistant | [apps.md](./apps.md#http-api) |
| `/api/gateway/engines/*` | local engine installs | [engines.md](./engines.md#api-contract-gateway_engines_v2) |
| `/api/gateway/models/download*`, `/models/downloads*` | model download jobs and their event stream | [model-downloads.md](./model-downloads.md) |
| `/api/gateway/host/*` | host state, pause, restart, update, tray | [Host state](#host-state-and-model-residency), [Host control](#host-control-pause-desktop-tray-restart-update) |
| `/api/gateway/backlog/*`, `/reports/*`, `/triage/*`, `/processes` | operator tooling | [maintenance.md](./maintenance.md) |
| `/api/gateway/entities/*` | summoned entities | [entities.md](./entities.md) |

## Auth

By default, `/api/gateway/*` is protected by `GatewaySecurityMiddleware` (bearer token + origin allowlist).  
See: [security.md](./security.md).

All examples below assume:

```bash
export BASE_URL="http://127.0.0.1:8080"
export AUTH="Authorization: Bearer $(cat "$ABSTRACTGATEWAY_DATA_DIR/auth/bootstrap-admin-token")"
```

## Provider connections

Gateway-owned provider connections let users create reusable cloud, local, or
OpenAI-compatible endpoints without putting raw API keys in workflow JSON or
browser storage. The API route is named `provider-endpoint-profiles`; the
console presents them as provider connections.

- `GET /api/gateway/config/provider-endpoint-profiles`: list visible profiles.
- `POST /api/gateway/config/provider-endpoint-profiles`: create a user- or
  admin-owned profile.
- `POST /api/gateway/config/provider-endpoint-profiles/discover-models`:
  discover models for a draft or saved profile by calling the configured
  provider family and base URL with the entered or server-side key. The raw key
  is never returned.
- `PUT` or `DELETE /api/gateway/config/provider-endpoint-profiles/{profile_id}`:
  update or delete a profile.

Enabled profiles appear in `GET /api/gateway/discovery/providers` as virtual
providers such as `endpoint:office-vllm`. Model discovery through
`GET /api/gateway/discovery/providers/{provider_name}/models` returns either the
fixed profile allowlist or the live endpoint model catalog.

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

Every start answers `{run_id, runner_warning, resolved_workflow}`.
`resolved_workflow` names the workflow the run really runs:
`{workflow_id, bundle_id, bundle_version, flow_id, registry_scope, name,
source: "gateway_default" | "client", interface}`. The same object is kept in
the run's inputs as `input_data.workflow_selection` (written by the gateway;
a value sent by the client is replaced), so `GET /runs/{run_id}/input_data`
tells a restored conversation how its workflow was chosen.

Evidence: request/response models live in `src/abstractgateway/routes/gateway.py` (`StartRunRequest`, `start_run`).

#### The gateway default agent workflow (`flow_id: "@default"`)

An agent client (AbstractCode, the Assistant, the Telegram bridge) can let
the gateway choose the workflow for an agent interface:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"flow_id":"@default","interface":"abstractcode.agent.v1","input_data":{"prompt":"Hello"}}' \
  "$BASE_URL/api/gateway/runs/start"
```

- `interface` is required with `@default` (400 without it); `bundle_id` and
  `bundle_version` are not accepted with it.
- The default is resolved at every start, so a change applies to the next
  new turn of any conversation that sends `@default`.
- When the default cannot run (its workflow is gone, deprecated, or does not
  declare the interface), the start is refused with 409 and a message naming
  the setting (`agents.default_workflow.<interface>`) and where its value
  comes from. The gateway never quietly runs another workflow instead.
- `POST /runs/schedule` accepts the same `flow_id: "@default"` + `interface`
  (the schedule then targets the version resolved at that moment).

What the default is for each interface (readable without admin rights):
`GET /bundles` and `GET /workflow-catalog` carry

```json
"default_agent_workflows": {
  "abstractcode.agent.v1": {"workflow_id": "basic-agent@0.0.5:81795ea9", "bundle_id": "basic-agent",
                            "bundle_version": "0.0.5", "flow_id": "81795ea9", "registry_scope": "private",
                            "name": "basic-agent", "source": "default"}
},
"default_agent_workflows_unavailable": {
  "abstractassistant.agent.v1": {"source": "default", "value": null,
    "reason": "no host workflow declares abstractassistant.agent.v1; the Assistant uses its built-in orchestrator"}
}
```

and every entrypoint row carries `is_agent_default` and
`agent_default_interfaces`. These are different from `default_bundle_id`
(the bundle a bare `flow_id` falls back to), from a catalog record's
`is_default` (its default version) and from a bundle's `default_entrypoint`.

The setting itself is `agents.default_workflow.<interface>` (see
[Configuration](configuration.md#default-agent-workflow)).

For VisualFlow bundles, Gateway runs the packed JSON through AbstractRuntime.
Structured LLM/Agent schemas are Runtime/Core-owned: `response` remains textual,
and schema-conformant object values are available through the node `data` output
for data edges such as Break Object and Switch.

#### Durable session replay (`use_session_history`)

Thin clients do not need to carry conversation transcripts. Passing
`"input_data": {"use_session_history": true}` together with a `session_id`
makes the gateway seed the run's `context.messages` from the session's prior
COMPLETED root runs before the run starts: the run store is the durable
transcript.

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"bundle_id":"my-bundle","session_id":"sess-1","input_data":{"prompt":"and what did I say before?","use_session_history":true}}' \
  "$BASE_URL/api/gateway/runs/start"
```

Rules (the model-vs-display divergence contract — what the model replays is
deliberately narrower than what history views display):

- Client-provided non-empty `context.messages` always win; the seed never
  overwrites them. An EMPTY client `context.messages` list does not count as
  a transcript — the seed still runs (use the cap below to disable).
- Only COMPLETED root runs of the session contribute, as strictly alternating
  user/assistant pairs. FAILED and CANCELLED turns are invisible to replay by
  design (a promptless answer or answerless prompt would seed a dangling
  message and invite re-answering a stale ask); history views still show them.
- Steering/operator guidance injected mid-run is not replayed; over-long
  messages are truncated with a labeled `#TRUNCATION` marker; whole oldest
  turns are dropped first (`session_history_max_chars` cumulative budget).
- Caps: `input_data.session_history_max_messages` (1..200; explicit `0`
  disables replay for the run) > `ABSTRACTGATEWAY_SESSION_HISTORY_MAX_MESSAGES`
  > default 40. Chars: `session_history_max_chars` >
  `ABSTRACTGATEWAY_SESSION_HISTORY_MAX_CHARS` > default 24000.
- Failures degrade to a labeled `_runtime.session_history` `#FALLBACK` note
  and an unseeded start — never a blocked run. Success records
  `_runtime.session_history = {seeded: N, ...}` on the run for observability.
- Entity lanes never ride this: their transcript authority is the entity home
  (`_visit.history` / the chat driver), not the run store.

Evidence: `_seed_session_history` in `src/abstractgateway/hosts/bundle_host.py`
and `abstractruntime.session_history.session_chat_messages`.

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

### 2c) Shared workflow catalog

Private `/api/gateway/bundles` routes are scoped to the signed-in user's routed
runtime, and you may change the registry you own. The gateway's own bundle
directory is shared by every user, so writing it — upload, delete, reload,
deprecate, and `POST /visualflows/{flow_id}/publish` — requires an admin
principal and otherwise returns `403`. Listing and running are unaffected. See
[security.md](./security.md) for the full rule.

Shared/default workflows use the Gateway workflow catalog instead:

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/workflow-catalog"
```

Admin-only catalog operations live under
`/api/gateway/admin/workflow-catalog/*`:

- upload or promote immutable `.flow` versions;
- move a bundle's default pointer;
- set ACLs;
- deprecate, block, or tombstone a version without deleting bundle bytes.

Start a catalog workflow in the requesting user's runtime by setting
`registry_scope`:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"registry_scope":"tenant_catalog","bundle_id":"basic-agent","flow_id":"root","input_data":{"prompt":"Hello"}}' \
  "$BASE_URL/api/gateway/runs/start"
```

If `bundle_version` is omitted, Gateway uses the admin-managed catalog default
pointer. Exact older versions keep working until that specific version is
deprecated, blocked, or tombstoned.

Catalog scope is explicit: omitting `registry_scope` starts only private
runtime bundles. Flow/schema inspection for catalog workflows should use the
ACL-aware catalog endpoints:

- `GET /api/gateway/workflow-catalog/{bundle_id}/versions/{bundle_version}/flows/{flow_id}`
- `GET /api/gateway/workflow-catalog/{bundle_id}/versions/{bundle_version}/flows/{flow_id}/input_schema`

`framework_catalog` is reserved but not loadable yet; use `tenant_catalog`.

### 2d) Docs Q&A (`docs-qa` catalog bundle)

`docs-qa` is the shared transport for docs-grounded assistant panels (the
unified top-bar drawers). The contract: the CALLER supplies its own corpus
(typically its `llms.txt` text) — the bundle never guesses one, so answers are
never silently grounded on another app's docs.

Fresh installs need no manual publish: the gateway ships `docs-qa` in the
wheel and boot idempotently publishes it into the tenant catalog
(publish-if-absent by exact version; an admin's default pointer, tombstones,
and publisher attribution are never touched; publisher `system:gateway-boot`).
The publish is skipped on custom-bundle deployments whose private registry
carries no LLM-bearing flow (it would add a boot requirement they never had)
and can be disabled with `ABSTRACTGATEWAY_AUTO_PUBLISH_SHIPPED=0` — the
manual upload below then remains the path.

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" -d '{
  "registry_scope": "tenant_catalog",
  "bundle_id": "docs-qa",
  "bundle_version": "0.1.0",
  "flow_id": "docsqa001",
  "input_data": {
    "question": "How do I publish a workflow bundle?",
    "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
    "docs": "<your llms.txt text>",
    "app": "MyApp"
  }
}' "$BASE_URL/api/gateway/runs/start"
```

Then poll `GET /runs/{run_id}` (or stream the ledger); the answer is
`output.response`. `provider`/`model`/`temperature` may ride `input_data` to
override gateway defaults. Answers cite section headings and say plainly when
the docs do not answer — the bundle refuses to invent endpoints or behavior.
Docs Q&A must never route through entity chat (a visit is billable and forms
memories).

### Run-level skills selection

`input_data.skills` (a list of skill NAMES) attaches curated skills to any
run started through `/runs/start`:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" -d '{
  "bundle_id": "basic-agent",
  "input_data": {"prompt": "…", "skills": ["agora-collaboration"]}
}' "$BASE_URL/api/gateway/runs/start"
```

Trust semantics (the same abstractskill gate as `GET /skills` and the
workforce spawn lane — one gate, never a second resolver): VALIDATED skills
activate and their index lands in the run's `_runtime.skills_block`
(byte-stable for the whole run) with the `read_skill` tool made reachable;
UNVERIFIED skills are held; advisory-BLOCKED skills never ride. Every
outcome is recorded as a labeled verdict in `_runtime.skills_resolution`
(`requested`/`active`/`verdicts`/`resolved_tree_hashes`) — nothing is
silently dropped. Agent-node subruns inherit the block verbatim with
`read_skill` appended to explicit child allowlists (empty allowlists keep
registry defaults). A caller-supplied `_runtime.skills_block` is never
overwritten; the selection is then ignored with a labeled verdict.

The gateway serves its OWN corpus for the console drawer:

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/docs/corpus"
```

Returns `{app, source, chars, text}`. Resolution order: the
`ABSTRACTGATEWAY_DOCS_CORPUS` env override first (set-but-missing is an honest
404 naming the checked candidates, never a silent fallback), then the repo
`llms.txt` in dev checkouts, then the corpus packaged with the wheel.

### Skills shelf

`GET /api/gateway/skills` lists the skills of the gateway's shelf:
`{skills: [...], shelf, shelf_source, bundled_version, warnings}`.
`shelf_source` says where the shelf comes from: `stored` (the saved setting
`skills.shelf`), `env` (a legacy launch environment value), `seeded` (the
gateway's own copy in `<data dir>/skills/registry`, kept up to date from the
curated shelf that ships with AbstractSkill at each start), `checkout` (a
framework checkout, used only when the gateway's own copy is missing) or
`none`. An empty list always comes with a warning that says why and what to
do; warnings are plain sentences meant to be shown as they are.

`POST /api/gateway/admin/skills/reseed` (admin) refreshes the gateway's own
copy now and answers the seed report (`added`, `updated`, `unchanged`, the
`kept_*` lists with what was kept and why, `bundled_version`,
`previous_version`). An edit made in that folder is never overwritten.

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

## A run's workspace folder (browse and preview)

A run works in a folder on the gateway computer: the conversation's own
folder the gateway made (`<data dir>/workspaces/session-…`), or the folder the
client was started from. Three routes let the person who started the run see
it; another user's run id answers 404.

`GET /api/gateway/runs/{run_id}/workspace`:

```json
{"run_id": "…", "workspace_root": "/Users/me/Library/Application Support/abstractgateway/workspaces/session-chat-1-3f2a…",
 "kind": "session", "session_id": "chat-1", "exists": true,
 "host": {"hostname": "studio.local", "caller_is_this_machine": true},
 "open_supported": true}
```

`kind` is `session`, `run` or `launch_folder`. `open_supported` is true only
for an admin sitting at the gateway computer, the only caller for whom
`POST /runs/{run_id}/workspace/open` can open the folder; elsewhere show the
path and the host name.

`GET /api/gateway/runs/{run_id}/workspace/files?path=<folder>&recursive=false&limit=<n>`:

```json
{"path": "src", "entries": [{"name": "a.py", "path": "src/a.py", "type": "file", "size_bytes": 11, "mtime": "2026-09-25T10:00:00Z"}],
 "truncated": false, "limit": null, "recursive": false,
 "hidden": {"outside_links": 0, "blocked": 0, "other": 0}}
```

Folders come first, then files, by name. `limit` is optional; when the listing
stops there, `truncated` is true. `hidden` counts what is not shown: links
that lead outside the folder, and entries the workspace deny list blocks.

`GET /api/gateway/runs/{run_id}/workspace/content?path=<file>` streams the
whole file with its content type, `Content-Disposition: inline`,
`X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`, and
honours `Range` (206; 416 outside the file).

Paths are relative to the folder: absolute paths and `..` are refused (400),
a link that leads outside is refused (403), the gateway's own marker file is
never listed nor served. The built-in deny list (credential folders such as
`~/.ssh`, and the gateway's data folder; see
[Configuration](configuration.md#workspace-policy-filesystem-scope)) is never
listed, and reading inside it answers 404, even when the run's folder
contains it. The workspace access policy (allowed and blocked
folders, launch-folder trust) is applied again at every call, and nothing
else in the gateway's data folder is ever served. A run cannot be started
with a `workspace_root` inside the gateway's data folder either, except the
conversation folder the gateway made for the same user.

## Artifacts and filesystem handoff

Gateway artifacts are the cross-package representation for files, media, and
large payloads. Thin clients should pass artifact refs across runs instead of
raw bytes or local paths:

```json
{
  "$artifact": "abc123",
  "artifact_id": "abc123",
  "run_id": "session_memory_sess-1",
  "content_type": "image/png",
  "filename": "input.png"
}
```

Gateway uses three distinct file-like source terms:

- `Artifact`: a durable runtime-owned payload reference.
- `Local File`: a browser/client upload source. Hosted clients should upload
  bytes; browser-local paths are never interpreted as server paths.
- `Server File` / `Server Folder`: user-facing wording for a workspace-scoped
  server path under Gateway policy. The engineering contract is the canonical
  `WorkspacePath` string returned by `/files/*`, artifact import/export, and
  Runtime file nodes.

Hosted local uploads stay artifact-backed:

- one local file upload creates one artifact ref;
- multiple local files create an ordered list of artifact refs in Flow;
- a local folder uploads one artifact per file and may send `source_path`
  (for example `reports/2026/summary.md`) so relative member paths survive in
  artifact provenance without exposing browser-local absolute paths.

Upload a local file or folder member:

```bash
curl -sS -H "$AUTH" \
  -F "session_id=sess-1" \
  -F "source_path=reports/summary.md" \
  -F "file=@./summary.md" \
  "$BASE_URL/api/gateway/attachments/upload"
```

List run artifacts:

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/runs/<run_id>/artifacts"
```

List artifacts visible to a session:

```bash
curl -sS -H "$AUTH" "$BASE_URL/api/gateway/sessions/sess-1/artifacts"
```

Browse server workspace files/folders:

```bash
curl -sS -H "$AUTH" \
  "$BASE_URL/api/gateway/files/list?path=&include_directories=true&limit=200"
```

Optional filters:
- `path`: browse a specific workspace folder or mount alias.
- `recursive=true`
- `family=image|video|audio|document|text|code|json|archive|other`
- `extensions=png,jpg` or newline-separated values
- `query=substring`
- `max_depth=<n>`

Search artifacts across Gateway storage:

```bash
curl -sS -H "$AUTH" \
  "$BASE_URL/api/gateway/artifacts/search?scope=all&artifact_kind=image&query=logo&tags=pin_id=image&include_stats=true&limit=500"
```

`scope` can be `all`, `session`, or `run`. Use `session_id` with
`scope=session` and `run_id` with `scope=run`; omit both for `scope=all`.
Search responses carry the row fields and also include
`artifact_envelope_v1`, a normalized projection of Runtime-owned descriptors,
access stats, and Gateway action links.

Useful query parameters:
- `artifact_kind`: UI-oriented kind filter. Comma-separated values match
  `semantic_kind`, `render_kind`, or `modality`; generic `audio` means
  unclassified audio and does not match canonical `voice`, `music`, or `sound`.
  Single canonical kinds such as `music`, `voice`, `image`, `markdown`, or
  `json` map to Runtime catalog filters. Multi-kind unions are supported, but
  may be Gateway post-filters until Runtime exposes OR filters.
- `semantic_kind` / `render_kind`: canonical descriptor filters when the caller
  wants the two dimensions separately.
- `modality`, `content_type`, `workflow_id`, `node_id`, `created_after`,
  `created_before`, and `tags`: server filters for indexed descriptor fields.
- `query`: case-insensitive metadata search. Gateway may post-filter this field
  when Runtime cannot index it directly.
- `include_stats=true`: include exact `stats.total`, byte totals, and facet
  counts for the selected server-side filter set, independent of `limit`.
- `limit`, `offset`, and `cursor`: bounded paging. The default Runtime Explorer
  page size is 500; `limit<=0` is bounded unless `debug_unlimited=true` is used
  by an admin/debug caller.

`artifact_envelope_v1` contains normalized fields such as `semantic_kind`,
`render_kind`, `workflow_id`, `node_id`, `turn_id`, `ledger_cursor`,
`generation`, `producer`, `media`, `source_refs`, `access`, and `links`.
Sparse producer metadata is represented as missing fields; Gateway does not
invent provider/model provenance from filenames.

Generated-media artifacts created by child runs and projected into the parent
run preserve Runtime descriptors and structured metadata. Direct transcription
routes store transcript artifacts with source-audio refs, language/prompt hints,
provider/model when available, and bounded route parameters.
Descriptor-provided action links are sanitized to relative Gateway/UI links
before they appear in envelopes; raw external provider URLs should be represented
as trace availability or Gateway-owned trace records.

Content reads can label the access type for Runtime access stats:

```bash
curl -sS -H "$AUTH" \
  "$BASE_URL/api/gateway/runs/<run_id>/artifacts/<artifact_id>/content?access_action=preview"
```

Supported access actions are `content`, `preview`, and `download`. The shorter
`access=preview` alias is also accepted.

Import a server workspace path into a session artifact:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"session_id":"sess-1","source":{"kind":"workspace_path","path":"inputs/photo.png"},"pin_id":"image"}' \
  "$BASE_URL/api/gateway/artifacts/import"
```

Export an artifact back into the server workspace:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"path":"outputs/photo.png","create_parent_dirs":true,"overwrite":false}' \
  "$BASE_URL/api/gateway/runs/<run_id>/artifacts/<artifact_id>/export"
```

Import and export use the same Gateway workspace policy as file helpers:
workspace roots, mounted roots, ignored paths, and size limits are enforced on
the server. Browser-local files should be uploaded through
`POST /api/gateway/attachments/upload`; browser-local file paths are not
interpreted as Gateway workspace paths. In hosted user-auth mode, server
workspace import/export and `/files/*` helpers require an admin principal.
Ordinary users can still upload browser-local files and list/search artifacts in
their own routed runtime.

Canonical Gateway server paths use `rel/path` for the main workspace root and
`mount_alias/rel/path` for approved mounts. When two allowed mounts share the
same basename, Gateway emits deterministic digest-suffixed aliases so the same
public path string can round-trip through `/files/*`, artifact import/export,
and Runtime file nodes.

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
- Tools (thin-client allowlist help): `GET /api/gateway/discovery/tools`
- Skills inventory: `GET /api/gateway/skills` — the abstractskill shelf with
  trust verdicts (roster rows `{name, description, trust_level, blocked,
  requires_review, tree_hash, source, has_scripts, reasons}`); degradations
  are labeled `warnings`, never a fabricated list. Shelf resolution:
  `ABSTRACTGATEWAY_SKILLS_SHELF`, else the triage repo's
  `abstractskill/registry`.
- MCP server inventory: `GET /api/gateway/mcp/servers` — the declared
  registry at `<data_dir>/config/mcp_servers.json`
  (`{"version": 1, "servers": [{"name", "url"?, "description"?,
  "auth_required"?, "tags"?}]}`), served with declared fields only and
  `probed: false` (connect state/tool counts require a probe lane and are
  never faked).
- Dynamic capability catalogs: `GET /api/gateway/voice/voices`, `GET /api/gateway/audio/speech/models`, `GET /api/gateway/audio/transcriptions/models`, `GET /api/gateway/audio/music/providers`, `GET /api/gateway/audio/music/models`, `GET /api/gateway/vision/provider_models`

The capabilities payload includes package presence (`abstractruntime`,
`abstractcore`, `abstractmemory`, `abstractvoice`, `abstractvision`), existing
gateway helpers (`tools`, `visualflow`, `media`), memory-store readiness, and
AbstractCore capability plugin status for `voice`, `audio`, `vision`, and
`music`.

The route paths and contract descriptors are the stable part of this surface.
Catalog routes also include a stable Gateway-owned envelope:

- `catalog.contract = gateway_catalog_v1`
- `catalog.version = 1`
- `items = [...]`

The lower-layer fields stay in the payload for compatibility. Thin clients
should read `catalog` plus `items`; the route-specific fields (`models`,
`provider_models`, `profiles`, `voices`) remain available.

Provider discovery also reports the resolved default provider/model when one is
configured. The resolver follows request values, flow pins, and the execution-host
`input.text` capability route; if no pair exists, the response includes
`default_error` rather than a hardcoded local model.

It also includes a versioned thin-client contract:

- `capabilities.contracts.version`: currently `1`
- `capabilities.contracts.common`: shared run start/list/summary/input/history,
  ledger, artifact, attachment, workspace, discovery, provider prompt-cache
  controls, and the host-visibility descriptors `model_residency` (including
  `row_schema = "model_residency_row_v1"` and the canonical `modality_ui`
  color map), `host_state`, and `session_caches`
  (see [Host state and model residency](#host-state-and-model-residency)).
  `common.artifacts` includes run listing/content, session artifact
  listing, artifact search with `artifact_envelope_v1`, exact stats/facets,
  `artifact_kind` UI filtering, workspace import, and workspace export
  descriptors when available. Permission-sensitive descriptors are principal-aware:
  ordinary users see admin-only workspace import/export and provider
  prompt-cache controls marked unavailable with `admin_required` metadata.
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

### Native-loop bundles (react / codeact / memact)

Some shipped bundles declare `metadata.native_loop_factory` instead of VisualFlow
JSON (`manifest.flows` is empty). The gateway materializes an abstractagent
loop at load time. Discovery uses the same bundle list endpoint — **not**
`/discovery/workflows`.

Thin clients should:

1. `GET /api/gateway/bundles` (authenticated).
2. Filter entrypoints whose `interfaces` includes `abstractcode.agent.v1`.
3. Read `metadata.native_loop_factory` (`react`, `codeact`, or `memact`) to
   distinguish native loops from VisualFlow agent bundles.
4. Use each entrypoint's `workflow_id` (for example `react-agent@0.1.0:react`).
5. Start runs with `POST /api/gateway/runs/start` and
   `bundle_id` / `flow_id` from the bundle listing (for example
   `react-agent` + `react`).

Native-loop entrypoints do not ship VisualFlow JSON. The gateway serves a
versioned input-schema stub (`prompt` required; `provider` and `model`
optional) from
`GET /api/gateway/bundles/{bundle_id}/flows/{flow_id}/input_schema`.
Headless clients may also pass those fields without fetching the schema.

The shipped `react-agent@0.1.0` bundle is built by
`scripts/build_react_agent_bundle.py` and force-included in the wheel. A running
gateway process must restart (or call bundle reload) after the file lands on
disk before `/bundles` lists it.

### Run history bundle (`GET /runs/{run_id}/history_bundle`)

Thin clients should prefer this endpoint over stitching ledger, session, and
artifact endpoints. The export is owned by AbstractRuntime; the gateway forwards
query parameters and returns the bundle JSON unchanged (including in-band
degradations).

Query parameters:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `include_subruns` | `true` | Descendant runs in the bundle tree |
| `include_session` | `false` | Root session turn list |
| `session_turn_limit` | `200` | Cap when `include_session=true` |
| `ledger_mode` | `tail` | `tail` or `full` |
| `ledger_max_items` | `2000` | Per-run ledger cap when `ledger_mode=tail` |
| `detail` | `full` | `full` (complete payloads) or `replay` (transcript-fold projection) |

**`detail=replay`** drops request-side payloads and observability paths the
transcript fold never reads; runtime marks each omission with `$omitted` inside
ledger records. Use it for session replay and thin-client folds — it is much
smaller than `full` (gzip helps further; send `Accept-Encoding: gzip`).

**`warnings`** (always present, may be empty): typed degradations the export
survived instead of failing silently. Each entry is an object with at least
`code` and `detail`; many include `run_id`. Known codes today:

| Code | Meaning |
|------|---------|
| `subtree_discovery_failed` | Could not list child runs; bundle covers root only |
| `subtree_truncated` | Descendant discovery hit the run cap |
| `ledger_read_failed` | Ledger for a run id could not be read |
| `torn_rows_skipped` | Corrupt/unparseable ledger lines skipped |
| `ledger_tail_window` | Ledger truncated to `ledger_max_items` (tail mode) |
| `input_data_offload_failed` | Input-data artifact reference could not be resolved |

Clients must surface non-empty `warnings` to the operator — a bundle that
"looks complete" but carries warnings may be missing subruns, ledger tail, or
offloaded input data.

### Session history bloc (`GET /sessions/{session_id}/history/bloc`)

Returns one cursor-bounded bloc of **root session turns**, each with an inline
`history_bundle` export — one round-trip instead of N per-turn bundle fetches
(laurent c5551). Resume pagination uses an ISO `created_at` cursor in the
`before` query parameter (never turn-count offsets).

Query parameters:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `before` | *(omit)* | ISO-8601 cursor; only turns strictly **before** this timestamp |
| `limit` | `5` | Max turns in this bloc (1–50) |
| `detail` | `replay` | Forwarded to each turn's bundle export (`full` \| `replay`) |
| `include_subruns` | `true` | Per-turn bundle tree |
| `ledger_mode` | `tail` | `tail` or `full` |
| `ledger_max_items` | `2000` | Per-turn ledger cap when `ledger_mode=tail` |
| `include_drafts` | `false` | Include draft-test root runs |

Response fields: `session_id`, `cursor_before` (echo of `before`), `cursor_after`
(oldest turn returned — pass as the next `before`), `older_remaining`, `warnings`,
and `turns[]` (`run_id`, `created_at`, `status`, `bundle` or `error`).

The editor observes runs with the core lifecycle endpoints above:
`/runs/start`, `/runs/{run_id}`, `/runs/{run_id}/ledger`,
`/runs/{run_id}/ledger/stream`, `/runs/ledger/batch`,
`/runs/{run_id}/input_data`, `/runs/{run_id}/history_bundle`, and
`/runs/{run_id}/artifacts`.

## Optional multimodal scope

Current direct Gateway endpoints:
- `POST /api/gateway/runs/{run_id}/voice/tts`
- `POST /api/gateway/runs/{run_id}/voice/tts/stream`
- `POST /api/gateway/runs/{run_id}/audio/transcribe`
- `POST /api/gateway/runs/{run_id}/images/generate`
- `POST /api/gateway/runs/{run_id}/images/edit`
- `POST /api/gateway/runs/{run_id}/images/upscale`
- `POST /api/gateway/runs/{run_id}/videos/generate`
- `POST /api/gateway/runs/{run_id}/videos/from_image`
- `POST /api/gateway/runs/{run_id}/music/generate`
- `GET /api/gateway/voice/voices`
- `GET /api/gateway/audio/speech/models`
- `GET /api/gateway/audio/transcriptions/models`
- `GET /api/gateway/audio/music/providers`
- `GET /api/gateway/audio/music/models`
- `GET /api/gateway/vision/provider_models`
- `GET /api/gateway/vision/adapters`

`/voice/tts` returns a durable audio artifact after synthesis. `/voice/tts/stream`
returns JSON Lines stream events for progressive playback when discovery advertises
`capabilities.contracts.assistant.voice.tts.streaming=true`; successful streams still
finish with a Runtime-owned child-run audio artifact.

The catalog endpoints proxy AbstractCore Server routes when
`ABSTRACTCORE_SERVER_BASE_URL`
is configured. Gateway uses explicit Core auth settings for that hop and never
reuses the Gateway bearer token as a Core/provider secret. Without a configured
Core server, the voice/model routes return bounded static descriptors from
Gateway and capability-package environment variables.

Each route adds:

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
than a provider-specific image client. The route creates a durable child run,
stores the generated image as a run artifact, and returns
`event_name="abstract.progress"` so thin clients can stream the child-run ledger
for progress:

- `run_id`, `request_id`, `prompt`
- optional `provider`, `model`, `size`, `width`, `height`, `format`, batch
  `count` / `n`, `seeds`, and ordered `lora_adapters`
- `image_artifact`: first generated image for compatibility
- `image_artifacts`: full ordered image artifact list for batch generation

`size`, `width`, and `height` are optional passthrough request overrides. Do
not inject a client-side default size. Different image providers/models accept
different size sets; when the client leaves dimensions unset, Runtime/Core lets
the configured backend use its default or `auto` behavior.

If the active workflow runtime already has an AbstractCore LLM client, the route
uses it. For tools-only workflows, the route can create a direct Runtime/Core
client from request `provider`/`model` or the execution-host capability route
default. Unsupported or unconfigured deployments return a structured `ok=false`
response instead of a failed run.

Gateway also exposes a direct image-edit sibling route:

- `POST /api/gateway/runs/{run_id}/images/edit`

The request uses a source `image_artifact`, optional `mask_artifact`, the same
provider/model and image backend selectors as image generation, plus optional
batch `count` / `n`, `seeds`, and ordered `lora_adapters`, and returns an
artifact-backed edited image. Batch responses also return `image_artifacts`.
Thin clients should feature-detect it from
`capabilities.contracts.flow_editor.media.edited_image` or
`capabilities.contracts.assistant.media.edited_image`. It uses the same
child-run `abstract.progress` progress contract as direct image generation.

Gateway also exposes a direct image-upscale sibling route:

- `POST /api/gateway/runs/{run_id}/images/upscale`

The request uses a run-visible source `image_artifact`, optional provider/model
selectors, and optional upscaler controls such as `scale`, `resolution`,
`softness`, `seed`, `quantize`, and `vae_tiling`; `resolution` may be a
shortest-edge integer or a scale factor such as `2x`. Thin clients should
feature-detect it from `capabilities.contracts.flow_editor.media.upscaled_image`
or `capabilities.contracts.assistant.media.upscaled_image`, list models with
`GET /api/gateway/vision/provider_models?task=image_upscale`, and stream the
returned child-run ledger for `abstract.progress` events.

Generated music follows the same direct child-run pattern. Thin clients should
discover it from `capabilities.contracts.flow_editor.media.generated_music` or
`capabilities.contracts.assistant.media.generated_music`, list providers/models
from the music catalog routes, and treat the returned `child_run_id` plus
`music_artifact` as the durable output handle.

Generated video also follows the direct child-run pattern:

- `POST /api/gateway/runs/{run_id}/videos/generate` uses the Runtime/Core
  `output.modality=video` / `task=text_to_video` contract and accepts optional
  batch `count` / `n`, `seeds`, ordered `lora_adapters`, and `flow_shift`.
- `POST /api/gateway/runs/{run_id}/videos/from_image` accepts a run-visible
  source `image_artifact`, accepts the same optional batch/adapter/video
  control fields, and uses `task=image_to_video`.
- Thin clients should discover these routes from
  `capabilities.contracts.flow_editor.media.generated_video` and
  `capabilities.contracts.flow_editor.media.image_to_video` (or the matching
  `assistant.media.*` entries), use
  `GET /api/gateway/vision/provider_models?task=text_to_video|image_to_video`
  for model catalogs, use `GET /api/gateway/vision/adapters` for compatible
  installed adapter catalogs, stream the returned `child_run_id` ledger for
  `abstract.progress` events, and read `video_artifacts` when batch generation
  is requested.

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
`query_text` requires a vector-capable backend plus the execution-host
`embedding.text` route; SQLite returns a clear 400 instead of pretending to
support semantic recall.

Capability discovery reports KG memory as available when AbstractMemory is
installed and the configured backend can be resolved. A fresh persistent store
does not need to exist yet; empty-store structured queries return an empty
result rather than making Flow authoring nodes unavailable.

## Models and engines

The gateway serves AbstractCore's models and engines payloads unchanged,
under `/api/gateway`. The bodies and payloads
are the same as AbstractCore's own `/acore/*` routes; `abstractcore` and
`abstractgateway` render them with the same screens.

| Method and path | Access | Body / query | Returns |
|---|---|---|---|
| `GET /host/profile` | user | `refresh=1` | `host_profile_v1` |
| `GET /engines` | user | `probe=1` | `gateway_engines_v2` rows (AbstractCore's detection plus the install plan and actions) with `install_allowed` and `install_policy`; see [engines.md](./engines.md) |
| `GET /engines/{id}` | user | `probe=1` | one engine row plus `install_allowed`; 404 for an unknown id |
| `POST /engines/{id}/install` | admin | `{"dry_run": bool, "force": bool, "location": "auto"\|"user"\|"system"}` | an `engine_install_job_v1` job (user-level first; pauses in `needs_admin` / `needs_tools`), see [engines.md](./engines.md) |
| `GET /engines/jobs`, `GET /engines/jobs/{id}` | user | | engine install jobs |
| `POST /engines/jobs/{id}/continue`, `/cancel` | admin | `{"action"?}` | the job |
| `POST /engines/{id}/start`, `/stop` | admin | | Ollama / LM Studio server state |
| `GET /models/catalog` | user | `q`, `engine`, `fits=1`, `hub=1`, `tag` (repeatable) | `model_catalog_v1` |
| `GET /models/installed` | user | `provider` | `models_installed_v1` |
| `POST /models/download` | admin | `{"provider", "artifact", "dry_run", "expected_bytes"?}` or `{"recommended": true}` | `{"ok": true, "job": {...}}`; with `recommended`, `{"ok": true, "recommended": true, "jobs": [...], "group": {...}}` |
| `GET /models/download/{job}` | user | | `{"ok": true, "job": {...}}` (a `grp_...` id returns the parent job) |
| `GET /models/downloads` | user | | `{"ok": true, "jobs": [...]}`, newest first, parents included |
| `POST /models/download/{job}/cancel` | admin | none | `{"ok": true, "job": {...}}`; stops the transfer within about a second; a `grp_...` id cancels every running child; 404 when unknown |
| `GET /models/downloads/stream` | user | `job_id`, `until_idle=1` | Server-Sent Events of the same dicts, see [model-downloads.md](./model-downloads.md) |
| `POST /models/delete` | admin | `{"provider", "artifact", "dry_run": bool, "force": bool}` | `host_job_v1` (kind `delete`) |
| `GET /jobs` | user | `kind`, `status` | `{"schema": "host_jobs_v1", "jobs": [...], "generated_at"}`, newest first |
| `GET /jobs/{id}` | user | | `host_job_v1`; 404 when unknown |
| `POST /jobs/{id}/cancel` | admin | none (an empty `{}` is accepted) | `host_job_v1`; 404 when unknown |

Empty query values (`q=`, `engine=`) mean "no filter". `probe`, `fits` and
`hub` accept `1`/`0` and `true`/`false`.

**Catalog artifacts.** `model_catalog_v1` is AbstractCore's payload, served
unchanged (field reference: AbstractCore `docs/models.md`, "The catalog").
Besides `quant` (the artifact's own label, lowercased, or `null`) and `bits`
(effective bits per weight), every artifact carries `quant_class`, one of
`2bit`, `3bit`, `4bit`, `5bit`, `6bit`, `8bit`, `16bit`, `full`, `unknown`,
for filtering by quantization: `q4_k_m`, `4bit`, `mxfp4` and `oq4e` are
`4bit`; `q8_0` and `8bit` are `8bit`; `bf16` and `f16` are `16bit`; `f32` is
`full`. `quant_class_source` is `stated` (the reference names its quant),
`assumed` (a bare Ollama tag such as `qwen3.5:9b` or LM Studio id: the class of
the engine's default build, which the fit estimate assumes too) or `null` (no
quant information; the class is `unknown`). `options` holds the route options a
recommendation copies with the artifact (`{}` for most); `companions` lists
repos downloaded with it (an MLX build's MTP drafter, from AbstractCore's
drafter registry; `[]` for most), `companion_bytes` is their size, and
`download_bytes` already includes it; `note` is one sentence about the build. On Apple silicon the text rows pre-select the
memory tier's MLX build and exactly one text row is the `starter`.

**Jobs.** A `host_job_v1` has `schema`, `job_id`, `kind`
(`download | delete | engine_install`), `status`
(`queued | running | completed | failed | cancelled`), `provider`, `artifact`,
`engine`, `percent`, `downloaded_bytes`, `total_bytes`, `message`, `log_tail`,
`command` (the exact argv), `dry_run`, `started_at`, `finished_at`, `error`
(a string or `null`), `joined`, `result` and `cli_equivalent`, which names the
`abstractgateway` command that does the same thing. A dry run finishes before
the POST returns. On the `/models/download` routes the job also carries
`job` (the id), `events`, `host_status`, reports `queued` as `running`, and
counts `joined` including the first request.

**Download progress.** A download job also carries `state`
(`queued | resolving | downloading | verifying | installing | done | failed |
cancelled | stalled`), `bytes_done`, `bytes_total`, `size_unknown`,
`size_note`, `bytes_per_second`, `eta_s`, `updated_at`, `files`
(`[{name, bytes_done, bytes_total, state}]`), `current_file`, a one-sentence
`message`, the tool's own `detail`, and `transitions`. "Use recommended
defaults" (`{"recommended": true}`) returns one parent job (`kind:
"download_group"`, id `grp_...`) whose bytes, percent, speed and time left
add up its children. The full contract, one real example per state and what
each source reports: [model-downloads.md](./model-downloads.md).

**Refusals** share one body:
`{"ok": false, "status", "reason"?, "message", "detail", "error": {"message", "type"}, ...}`.

| Status | When |
|---|---|
| 400 `invalid` | provider or artifact missing |
| 403 `refused` / `not_allowed` | a real engine install while `allow_engine_install` is off ([configuration.md](./configuration.md#allow_engine_install)); `install_policy` says why |
| 403 | the caller is not an admin (every POST above) |
| 404 `not_found` | unknown job id, engine id, or a model that is not installed |
| 409 `busy` | an engine install is already running (`job` is the running one) |
| 409 `refused` | the engine is not supported here or has no install command (`install` is the plan), or a delete is blocked (`delete_blockers`: `loaded`, `shared_cache:…`, `unknown_location`, `engine_not_running`, `remote_engine`; `force: true` overrides the first two) |
| 501 `unsupported` / `abstractcore_too_old` | the installed AbstractCore is too old for these routes; `required`, `installed` and `missing` name what to upgrade |
| 503 `unavailable` | AbstractCore is not installed |

Example:

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$GW/api/gateway/models/catalog?q=qwen3&fits=1" | jq '.rows[0].artifacts[0].fit'
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"dry_run": true}' "$GW/api/gateway/engines/ollama/install" | jq '.command, .cli_equivalent'
```

## Host state and model residency

Gateway exposes a host-level view of the execution machine — memory, GPU,
resident models, and session prompt caches — so consoles and agents can render
an "agentic OS" panel from one API surface.

Read endpoints (any authenticated principal):

- `GET /api/gateway/host/state` — one-call host snapshot
- `GET /api/gateway/host/metrics/memory` — host memory snapshot
- `GET /api/gateway/host/metrics/gpu` — GPU utilization probe
- `GET /api/gateway/models/loaded` — model residency listing
- `GET /api/gateway/models/context_estimate` — context/KV memory estimate for
  a provider+model
- `GET /api/gateway/sessions/prompt_cache` — session prompt-cache enumeration

Mutation endpoints (admin principal required):

- `POST /api/gateway/models/load` — load (and by default pin) a model runtime
- `POST /api/gateway/models/unload` — unload a model runtime
- `POST /api/gateway/models/lock` — lock a resident model against unload
- `POST /api/gateway/models/unlock` — release a model-residency lock
- `POST /api/gateway/models/download` — fetch model weights onto the host
- `POST /api/gateway/sessions/{session_id}/prompt_cache/clear_all` — clear
  every runtime-minted prompt cache for a session

Reads are visibility every authenticated client needs; mutations spend shared
host resources and stay operator acts. Anonymous requests are rejected on all
of these routes, like every other `/api/gateway/*` path.

### `GET /host/state`

One snapshot with `memory`, `gpu`, `models`, and `session_caches` sections:

```json
{
  "ok": true,
  "ts": 1787857000.0,
  "memory": {"ram": {"...": "..."}, "process": {"rss_bytes": 140443648}, "device": {"backend": "metal", "allocated_bytes": 0, "...": "..."}},
  "gpu": {"supported": true, "source": "ioreg", "gpus": [{"name": "...", "utilization_gpu_pct": 0.0}]},
  "models": [{"runtime_id": "...", "provider": "...", "model": "...", "resident": true, "...": "..."}],
  "session_caches": [],
  "totals": {"models": 2, "models_resident": 1, "model_bytes": 3109915433, "session_caches": 0, "session_cache_bytes": null},
  "degraded": [],
  "row_schema": "model_residency_row_v1"
}
```

- `models` rows use the frozen `model_residency_row_v1` schema described
  below; `session_caches` relays the runtime facade's cache rows verbatim.
- Every section is independently best-effort and the route never returns a
  500. A missing facade method or a failed probe nulls that section and names
  it in `degraded`; a `reasons` map (present only when non-empty) says why.
- The `gpu` section keeps its in-band `{"supported": false, "reason": "..."}`
  payload when the probe answers but reports no support; it still counts as
  degraded.
- `totals.model_bytes` sums the known `size_bytes` values and is `null` when
  no row reports a size; `totals.session_cache_bytes` behaves the same over
  the cache rows' `bytes`.
- `totals.models` counts every known row — configured / cached rows included —
  while `totals.models_resident` (additive) counts only rows with
  `resident: true`. Clients that display "N loaded" must read
  `models_resident`: default ≠ loaded, and presenting configured capability
  defaults as loaded is exactly the lie this field removes.
- When the runtime memory snapshot reports a host identity, the response also
  carries a top-level `host` object (the identity facts of the machine the
  snapshot describes). The block is omitted when the runtime does not report
  one. Together with the per-row `host_id`/`host_name` fields below, this is
  the seam a multi-machine resource pool would aggregate on; one gateway
  binds one runtime host, and the pool design is proposed in
  [backlog 0093](https://github.com/lpalbou/abstractgateway/blob/main/docs/backlog/proposed/0093_multi_machine_model_resource_pool.md).

### `GET /host/metrics/memory`

Returns `{"ok": true, "supported": true, ...}` plus the snapshot sections:
`ram` (total/available/used bytes and percent), `process` (`rss_bytes`), and
`device` (`backend`, `allocated_bytes`, `total_bytes`, `free_bytes`). When the
runtime host facade does not expose a memory snapshot, the route answers 200
with `{"ok": true, "supported": false, "reason": "..."}` — the same degraded
style as `GET /host/metrics/gpu`.

How to compare memory measurements: `process.rss_bytes` and
`device.allocated_bytes` are different axes. In-process device backends (for
example MLX on Metal) return freed buffers to the process heap and the
operating system may retain those pages, so process RSS does not shrink when a
model unloads. Use `device.allocated_bytes` to verify that an unload freed
device memory; use `ram` and `process` for overall host pressure.

### Model residency (`/models/loaded`, `/models/load`, `/models/unload`)

`GET /models/loaded` lists the model runtimes the host knows about, with
optional `task`, `provider`, `model`, and `base_url` query filters. The
response keeps the raw runtime records in `models` and adds a normalized
`rows` array in the frozen `model_residency_row_v1` schema (named by
`row_schema`), so thin clients do not need per-provider alias tables.

Each `model_residency_row_v1` row has exactly these fields (unknown values
are `null`, never guessed):

`runtime_id`, `task`, `provider`, `model`, `source`, `resident`, `state`,
`pinned`, `default`, `size_bytes`, `size_vram_bytes`, `expires_at`,
`context_length`, `loaded_at`, `last_used_at`, `locked`, `lockable`,
`modalities`, `calibrated_context_length`, `context_calibrated`, `host_id`,
`host_name`, `details`

The schema is additive-tolerant and keeps the `model_residency_row_v1` name
as optional fields are added; treat fields beyond the original 16 as
optional. The lock/calibration/host fields mean:

- `locked` / `lockable` — tri-state booleans: whether the model is locked
  against unload, and whether this runtime supports locking it at all.
- `modalities` — list of modality strings when the runtime reports one
  (`null` otherwise, including when the value is not a clean string list).
- `calibrated_context_length` / `context_calibrated` — the measured usable
  context length and whether it came from calibration rather than metadata.
- `host_id` / `host_name` — identity of the machine serving the model,
  stamped by the runtime that reported the row (see the `host` block note
  under `GET /host/state` above).

Residency truth is provider-first: `provider_resident` / `provider_loaded`
booleans in the source record outrank the runtime-lease booleans `resident` /
`loaded`, because a runtime can hold a lease on a model the provider has
already evicted. A loaded-looking `state` string (`provider_loaded`, `loaded`,
`resident`) can confirm residency, but a state string is never proof of
absence — with no boolean present and no loaded-like state, `resident` stays
`null`. `details` preserves the raw record for fields outside the schema.

`POST /models/load` accepts `task` (default `text_generation`), `provider`,
`model`, optional provider `options`, `pin` (default `true`), `base_url`,
`timeout_s`, and `lock` (default `false`) — with `lock: true` a successful
load is immediately locked against unload, and the lock outcome is reported
additively under `lock` in the response (a lock failure or a runtime without
lock support never turns the successful load into a failure). `POST
/models/unload` selects the runtime by `runtime_id` or by
`task`/`provider`/`model`. Both relay Runtime's host facade and return the
normalized residency response (`operation`, affected records, and in-band
`ok=false` errors instead of opaque failures).

One unload failure gets a real status code: when the target model is locked,
`POST /models/unload` answers **HTTP 409** with the normalized refusal payload
as the body (`ok: false`, `error: "model_locked"`, plus whatever detail the
runtime included), so clients can offer force-unload or point at
`/models/unlock`. Sending `"force": true` in the unload request unloads the
model despite the lock. Every other unload outcome stays in-band at 200.

### Model locks and context estimates

- `POST /models/lock` and `POST /models/unlock` (admin) pin a resident model
  against unload and release that pin. The body selects the target like
  unload does: `runtime_id`, or `provider` + `model`, with optional
  `base_url` and `timeout_s`. Lock requires provider-verified residency: a
  configured or merely-warm model refuses with an
  `error: "model_not_resident"` payload (load it with `lock: true` instead);
  unlock always works, even for a since-evicted model, so locks are never
  stranded. Rows report `lockable` so clients know whether
  a lock can work, and `locked` so they can render the current state.
- `GET /models/context_estimate?provider=&model=&context_length=` (any
  authenticated principal) relays the Runtime host facade's context/KV memory
  estimate for a provider+model. `provider` and `model` are required;
  `context_length` is optional and must be >= 1 (schema-rejected with 422
  otherwise). The estimate reports its `confidence` in-band — `calibrated`,
  `estimated`, or `unknown` — alongside facade fields such as
  `predicted_max_context` (the context that fits beside the weights), the
  tri-state `fits_weights` / `fits_requested_context` split, `budget_bytes`,
  `est_kv_bytes`, and `notes` (which state the budget basis and reserve).
  The estimate is advisory only — no load path gates on it.

Like the other host-facade relays, these routes never 500 on capability gaps:
a runtime without the method answers 200 with `ok: false`,
`available: false`, and `code = "model_residency_unavailable"` (lock/unlock)
or `code = "context_estimate_unavailable"` (estimate); facade exceptions use
the matching `*_error` codes.

### Session prompt-cache enumeration

- `GET /api/gateway/sessions/prompt_cache?session_id=<optional>`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/clear_all` (admin)

The list route enumerates the prompt caches the runtime actually minted. Each
cache row carries the provider/model/runtime identity, byte and token counts,
and stamped attribution metadata (`session_id`, `run_id`, `workflow_id`,
`node_id`). Omit `session_id` to list every session's caches. This
enumeration lane is the recommended way to observe and reclaim session cache
state: unlike the identity-derived session lifecycle endpoints described
under the prompt-cache control plane below, it cannot miss caches whose keys
the gateway never derived.

`clear_all` unloads every runtime-minted cache for one session in a single
call. It requires an admin principal because it accepts any session id and
clears real provider cache state; the identity-derived, caller-scoped session
lifecycle endpoints remain user-level.

When the runtime facade does not expose enumeration, both routes answer 200
with `ok=false`, `available=false`, and `code="session_caches_unavailable"`
(facade errors use `code="session_caches_error"`); the list route always
carries a `caches` array and `clear_all` always carries `cleared` and `count`.

### Discovery descriptors

`GET /discovery/capabilities` advertises this surface under
`capabilities.contracts.common`:

- `model_residency`: `endpoints` (`loaded`, `load`, `unload`, `lock`,
  `unlock`, `context_estimate`), the per-task support map,
  `row_schema = "model_residency_row_v1"`, and `modality_ui` — the canonical
  modality color map (`{version: 1, colors: {...}}`, one `{color, label}`
  entry per residency task plus an `unknown` fallback) so every client
  renders the same modality palette instead of hardcoding its own. It is a
  rendering contract, not a runtime capability, so it is served even when the
  runtime facade is absent.
- `host_state`: `endpoints` (`state`, `memory`, `gpu`) plus
  `memory_available`. The state route itself always answers; per-section truth
  lives in the payload's `degraded` list.
- `session_caches`: `endpoints` (`list`, `clear_all`) plus `available`,
  reflecting whether the runtime facade supports cache enumeration.

Evidence: `src/abstractgateway/routes/gateway.py` (`host_state`,
`host_memory_metrics`, `model_residency_loaded`, `model_residency_lock`,
`model_context_estimate`, `session_prompt_caches_list`) and
`src/abstractgateway/security/authorization.py` (route-family policy).

## About (`GET /api/gateway/about`)

Public (no sign-in): which versions this gateway runs, for About screens.

```json
{"abstractframework": "0.3.3", "abstractgateway": "0.4.4",
 "packages": {"abstractcore": "2.15.3", "abstractruntime": "0.4.36", "abstractskill": "0.3.0"}}
```

`abstractframework` is null when the framework meta-package is not installed
on the gateway computer. Versions only: no paths, host names or settings.

## Host control (pause, desktop tray, restart, update)

The process's own controls — the surface behind the desktop tray icon and
the console's Gateway card (see [tray.md](./tray.md)). Reads are available to
any authenticated principal; writes require an admin principal.

- `GET /api/gateway/host/runner` — execution state:

```json
{"ok": true, "paused": true, "paused_at": "2026-09-05T06:38:26+00:00", "paused_by": "default/admin", "reason": "meeting",
 "inflight_ticks": 0, "scope": "workflow runner", "runner_in_process": true, "step_gate_supported": true,
 "runners": [{"status": "paused", "...": "..."}], "degraded": false,
 "capabilities": {"restart": true, "shutdown": true, "reason": null, "update_job_running": false}}
```

- `POST /api/gateway/host/pause` (body `{"reason": "..."}` optional) and
  `POST /api/gateway/host/resume` — both answer the payload above.
- `GET /api/gateway/host/metrics/live` — `{gpu, memory, runner}` in one call
  (1 s caches); `gpu`/`memory` carry the same in-band `supported` shape as
  `/host/metrics/gpu` and `/host/metrics/memory`.
- `GET /api/gateway/host/runs?limit=25&window_hours=24` — recent runs across
  every data plane on this host (admin; `/runs` answers only for the calling
  principal's plane). `{ok, items: [{run_id, workflow_id, label, status,
  created_at, updated_at, ledger_len, plane, started_epoch}], count, has_more,
  planes, skipped_entity_planes?, warnings?}`. `label` decodes a catalog
  workflow's internal id (`__catalog__v2__…<base64>`) to the name an operator
  uses. Root runs only; the gateway's own bookkeeping runs (`__`-prefixed, but
  never a catalog id) are excluded.
- `GET /api/gateway/host/tray` — `{dependencies_installed, install_hint,
  decision: {start, reason, hint}, supervisor: {running, ready, pid,
  exit_code, failure, log_path}, can_control}`. There is no setting: the icon
  is shown whenever this process and this desktop can hold it.
- `POST /api/gateway/host/tray/show` — retry the helper now (409 when this
  process cannot). No `hide` counterpart, by design.
- `POST /api/gateway/host/restart`, `POST /api/gateway/host/shutdown` —
  `{"ok": true, "restart": true, "requested_by": "...", "reason": "..."}`;
  409 with a plain reason when unsupported (`--reload`, embedded server, an
  update is installing).
- `GET /api/gateway/host/update`, `POST /api/gateway/host/update/check`,
  `POST /api/gateway/host/update/start` — `{current, install: {kind,
  upgradable, reason, command, extras}, check: {latest, update_available,
  offline, checked_at, error}, job: {state, log_tail, exit_code,
  restart_recommended, version_before, version_after}, restart_pending}`.
  `start` answers 409 when the install cannot be upgraded in place or a job
  is already running.

`GET /api/health` adds `"paused": true` while paused; `status` stays
`"healthy"`.

## Prompt-cache control plane (operator API)

The gateway exposes prompt-cache operator endpoints under `/api/gateway/prompt_cache/*`.
Provider prompt-cache controls affect process-local or remote provider state
and require an admin principal in hosted user-auth mode.

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
`template_id`, and `version`. The private hash also includes the authenticated
principal scope, so two hosted users using the same session id/provider/model do
not collide in a shared provider control plane; the returned `identity` remains
portable app-level data and does not expose that private scope. These routes
expose three honest modes:

- `unsupported`: provider/model does not expose prompt-cache support; responses include `supported=false`, `ok=false`, and capabilities.
- `keyed`: gateway returns a stable `runtime_hint`/`prompt_cache_key` for Runtime/Core injection, but does not claim module preparation occurred.
- `local_control_plane`: gateway uses supported provider operations such as `prepare_modules`, `fork`, `set`, `clear`, and `stats`.

`status` is read-only. `prepare` accepts optional modules (`system_prompt`,
`workflow_instructions`, `tools`, `pinned_attachments`) and returns either
provider operation results or a key hint. `rebuild` is clear-plus-prepare for
providers that expose clear controls.

These identity-derived endpoints only see caches whose keys the gateway
derived. To enumerate or bulk-clear the caches the runtime actually minted for
a session, use the recommended
[session prompt-cache enumeration lane](#session-prompt-cache-enumeration).

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
