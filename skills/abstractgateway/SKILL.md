---
name: abstractgateway
description: Operate AbstractGateway through its CLI and authenticated HTTP APIs for durable workflow runs, replay views, approvals, automations, model resources and optional inference services. Use when starting or diagnosing framework runs, connecting thin clients, handling ledger-backed waits, or choosing between native run APIs, Gateway /v1 and direct Core serving.
license: MIT
compatibility: Python 3.10+. A reachable Gateway and Gateway user credentials are required for run APIs. Local inference needs the matching Apple or GPU install profile and explicitly downloaded models.
metadata:
  package-version: "0.21.0"
---

# AbstractGateway

## Purpose and limits

Use Gateway as the authenticated HTTP control plane for durable AbstractRuntime
runs and framework clients. Start work through run APIs and render its canonical
replay view; the server owns history, state, policy and tool approvals. For
inference without a durable workflow, use optional Gateway `/v1` or direct Core
HTTP with that surface's own credentials. Their requests do not become native
Gateway workflow runs.

## Install and verify

This skill includes unreleased endpoint controls. Install the matching source
checkout at the AbstractGateway repository root:

```bash
python -m pip install -e .
# For local engines instead: -e '.[apple]' or -e '.[gpu]'
```

Check the installed distribution and the taught feature before operating it:

```python
from importlib.metadata import PackageNotFoundError, version

try:
    installed = version("abstractgateway")
    from abstractgateway.routes.core_endpoint import EndpointChange
    from abstractgateway.routes.gateway import StartRunRequest
except (ImportError, PackageNotFoundError) as exc:
    raise SystemExit(f"Install the matching Gateway checkout: {exc}") from None
if "direct_enabled" not in EndpointChange.model_fields:
    raise SystemExit("This Gateway lacks the direct HTTP control; use its matching docs/skill.")
if "conversation" not in StartRunRequest.model_fields:
    raise SystemExit("This Gateway lacks conversational starts; use its matching docs/skill.")
assert EndpointChange(direct_enabled=False).direct_enabled is False
print(f"abstractgateway {installed}; direct endpoint control available")
```

For a published installation, its installed-version docs take precedence over
this unreleased skill. Ask the operator to place a Gateway **user bearer token**
in `GATEWAY_TOKEN` locally, never in chat. Set `GATEWAY_URL` explicitly, for
example `http://127.0.0.1:8080`. Stop on the first `401`/`403` and correct the
identity, token or policy before another call. Gateway `/v1` API keys cannot
authenticate `/api/gateway/*`; Core server tokens belong to direct Core only.

```bash
curl --fail-with-body -sS "$GATEWAY_URL/api/health"
curl --fail-with-body -sS -H "Authorization: Bearer $GATEWAY_TOKEN" \
  "$GATEWAY_URL/api/gateway/bundles"
```

Health must contain a status; bundle discovery must return JSON with `items`.
An empty `items` list means no usable installed bundle, not a successful run.
Fix workflow setup before starting work. For your own local instance, start
`abstractgateway serve --no-tray`; first-run sign-in uses its printed one-time
console link. Starting it creates its data directory and auth state.

## Mental model

- **Gateway → Runtime → execution owner:** native clients submit runs/commands;
  Runtime persists effects and ledgers; Core executes model calls. The default
  on Linux, macOS and Windows is one supervised model-host child. Explicit
  `--model-host in-process` and development reload use in-process execution.
- **Bundles and selection:** discover installed entrypoints before choosing a
  bundle/flow. `flow_id: "@default"` requires an `interface`; the documented
  `abstractcode.agent.v1` default resolves to `basic-agent` when available.
  Starts return `resolved_workflow`, the selection actually used.
- **Views and waits:** `/runs/{id}/view` is `replay_view_v1` over the run tree.
  `interaction` may belong to a child; use its run and wait key. Commands are
  asynchronously accepted into a durable inbox. An acceptance receipt is not
  evidence that the wait ended.
- **Resources:** supported managed Core loaders share compatible weights for
  one canonical artifact within the owner process. Incompatible engines,
  placement or profiles refuse before a second allocation; contexts can still
  have separate KV. External services and arbitrary plugin/raw loaders are
  outside that admission contract.
- **Availability:** native RPC readiness does not require HTTP inference.
  Model-host pings occur every 5 seconds; three unanswered pings trigger
  recovery. Five restarts in 10 minutes exhaust the budget. The memory ceiling
  defaults to 80% of machine memory; `--model-host-memory-gb 0` disables it.
  A ping is not proof that a particular inference is progressing.
- **Errors:** check HTTP status and body. `503 service_starting` includes
  `Retry-After`; child restart may also return `503`. A child dying during a
  request fails it with `424` and is not automatically replayed. Deterministic
  encoding/schema errors are nonretryable. Run failures are durable statuses
  and errors, even when their state GET returns HTTP 200.

## Capability map

| Task | Entry point |
|---|---|
| Discover and start workflows | `GET /api/gateway/bundles`, `POST /api/gateway/runs/start` |
| Read/follow canonical state | `GET /api/gateway/runs/{id}/view`, `/view/stream` |
| Approve, resume, pause or cancel | `POST /api/gateway/commands` |
| Recurring/event workflows | `/api/gateway/automations`, `/trigger-sources` |
| Artifacts, sessions and history | run/artifact/session routes under `/api/gateway` |
| Accounts, workspace policy, provider connections | authenticated account/config/workspace routes; admin restrictions apply |
| Models, engines and capability defaults | `/models/*`, `/engines/*`, `/config/capability-defaults` |
| One calibrated decision about a text | `POST /api/gateway/runs/{id}/decisions`; `POST /v1/decisions` on Gateway `/v1` |
| Browser apps, console, CLI and tray | `/console`, `abstractgateway`, `/apps/*`, `/host/*` |
| Optional inference HTTP | Gateway `/v1`; direct Core listener controlled by `direct_enabled` |
| Host/process diagnosis | `GET /api/health`, admin `/host/*`, admin `/core-endpoint` |

## Follow a conversational task or entity visit

Set strict JSON `conversation: true` when starting a task, summoning a Flow work session,
or opening a canonical entity visit. Render only explicit `public_update` messages from
the replay view (tasks/Flow) or `visit/{id}/state.public_updates` (driver visits); never
promote reasoning or raw diary records into public speech.
For a pending permission request, `details.public_action` is an optional public
explanation bound to its actor run and exact `call_ids`. Preserve it through the
entity session gate, including narrowed tool subsets. It is the actor’s stated
purpose, not evidence that an action happened. Missing context stays missing.

Use `inject_guidance` payload `{guidance: "context and correction", display_text: "original human words"}`
for task/Flow corrections. Only explicit display text creates public human history. For driver
visits use `POST /entities/{name}/visit/{id}/guidance` with `{text, display_text?}` instead;
do not bypass the visit door with generic steering. `accepted` means queued, not adopted.
Merge visit state `public_updates`, `public_messages` and `public_questions` by their canonical
`order`; display a history omission notice when any `*_omitted` count is positive. Only this
safe question projection restores past questions, actual answers and attributed timeout/late
reply notices. Do not reconstruct it from home ledgers or convert a system fallback into user speech.

For a question, use its actual run and exact `question:<id>` key with nonempty `response`.
Tasks/Flow use the existing nested resume command; driver visits use canonical `/answer`
with `{wait_key, response, target_run_id?}` (the actual child if present). Tool approvals
remain on their existing routes; never send `approved` to question answers. Essential
questions wait indefinitely; clarifications may expire after 120–300 seconds into a declared
fallback with no human response. Paused work does not auto-continue. A late reply can be
retained without answering a newer question. Check refreshed state rather than assuming
HTTP acceptance means the actor used the words. API and entity docs give the full contract.

Run starts write run/ledger/artifact/history state and may call remote paid
providers or tools. Installs/downloads/configuration/host controls change shared
state. Gateway does not invent missing models or silently choose a replacement
workflow when an explicit/default selection is unavailable.

## Recipes

### Start a requested workflow and follow it

After successful health/auth/discovery checks, send a selected bundle and its
flow. Omit `flow_id` only when the bundle has one entrypoint or a declared
default. The placeholders must come from bundle discovery:

```bash
curl --fail-with-body -sS -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"bundle_id":"<BUNDLE_ID>","flow_id":"<FLOW_ID>","input_data":{"prompt":"<TASK>"}}' \
  "$GATEWAY_URL/api/gateway/runs/start"
```

Success returns a nonempty `run_id` and `resolved_workflow`. Save that id as
`RUN_ID`, then read `/api/gateway/runs/$RUN_ID/view`. Inspect `status`,
`messages`, `warnings` and `interaction`. Follow changes with:

```bash
curl --fail-with-body -N -H "Authorization: Bearer $GATEWAY_TOKEN" \
  "$GATEWAY_URL/api/gateway/runs/$RUN_ID/view/stream"
```

Handle `view` snapshots, `delta` updates, keep-alive comments and `done`; ignore
unknown events. Save each SSE event id. Reconnect with `Last-Event-ID`, or
`?after=<cursor.token>` after an initial GET. A `failed` or `cancelled` terminal
state is not a completed answer. An interaction requires the next recipe.

### Answer an approval once

Read the latest canonical view. Use `interaction.run_id` and
`interaction.wait_key`, which can refer to a child rather than `RUN_ID`. For
an explicitly approved tool batch, submit:

```json
{
  "command_id": "<NEW_UUID_FOR_THIS_ACTION>",
  "run_id": "<INTERACTION_RUN_ID>",
  "type": "resume",
  "payload": {"wait_key": "<WAIT_KEY>", "payload": {"approved": true}}
}
```

Send that JSON to `POST /api/gateway/commands` with Gateway authentication.
Reuse the same command id if retrying an ambiguous delivery; do not create
several approval actions. A receipt contains `accepted`, `duplicate`, `seq`.
Continue following the view until the interaction ends; do not hide the wait
solely because the POST was accepted. Use the view's proposed `command` for
other interaction kinds rather than inventing their answer payload.

### Ask a decision model

A decision model answers typed questions about a text with calibrated
probabilities and generates no text. Through a run (a durable child run; the
`output.decision` capability default answers when `model` is omitted):

```bash
curl --fail-with-body -sS -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"state":"<TEXT>","questions":{"urgent":"Does the sender need an answer today?","topic":["billing","support","other"]}}' \
  "$GATEWAY_URL/api/gateway/runs/$RUN_ID/decisions"
```

Read `ok`. A true answer carries `child_run_id`, `decision` (`model`, `answers`,
`usage`, `latency_ms`) and `answers` by question name: a `noul` answer is the
probability that the statement is true; a `choice` answer has `choice`,
`confidence` and `probabilities`; a `score` answer has `score`, `confidence`,
`legend` and `probabilities`. `ok: false` names the reason in `code`
(`decision_unavailable`, `decision_error`, `decision_no_answers`); inspect it,
do not retry blindly. The typed question form is `{type: choice|noul|score,
instructions, criteria}`; add `"model": "<provider/model>"` to choose the model
and `"truncate": true` to cut an over-long state instead of failing. Discover
the route first: `GET /api/gateway/discovery/capabilities` →
`contracts.assistant.decision.direct_endpoint.available`.

Through Gateway `/v1` (a Gateway `/v1` API key from the OpenAI API page, not the
Gateway token; Guest may call it when the API is in Open mode):

```bash
curl --fail-with-body -sS -H "Authorization: Bearer $GATEWAY_V1_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"kev-latest","state":"<TEXT>","questions":{"urgent":"Does the sender need an answer today?"}}' \
  "$GATEWAY_URL/v1/decisions"
```

AbstractCore answers `{model, answers, usage, latency_ms}`. `kev-latest` reuses
the decision model loaded most recently, else the configured `output.decision`
default (400 when there is neither; pass `provider/model`, for example
`mlx/jaredpalmer/kev-4b`); 404 means the model is not downloaded; 422 means the
state is over the model's context (`"truncate": true` cuts it).

### Inspect optional inference services

As an admin, GET `/api/gateway/admin/core-endpoint`. `enabled` controls Gateway
`/v1`; `direct_enabled` controls direct Core. Both default to false. Check
`http_services.legacy` and `.direct` (`state`, `url`, `error`) for actual
readiness, not just the switches. An empty status can mean disabled services
or no live owner status. Enable only the surface requested by the operator:
POST `{"direct_enabled": true}` or `{"enabled": true}` to the same route.

The direct listener uses Core's own configured bind and server token (default
bind `127.0.0.1:8000`). Changing either switch does not restart the model pool.
After correcting a listener startup error, toggle that service off/on to reset
its exhausted recovery budget. `stopping` means cleanup has not closed it yet.

## Rules and sharp edges

- Run starts, downloads and tools can spend money, take minutes or change files.
  Use read-only discovery first; execute them for the user's authorized task.
- Keep a single framework execution owner. Do not launch a second standalone
  Core server to repair a native chat failure; it has independent resources.
- Do not put credentials in workflow JSON or print them. Browser session auth
  requires its CSRF token for mutations; use the documented session contract
  instead of treating an account API key as a session.
- Keep canonical state on the server. Do not reconstruct approvals or final
  messages from raw storage encodings, or infer success from an HTTP receipt.
- Do not blindly resubmit after an inference/process failure: prior tools may
  already have effects. Inspect the run and incident first.

## When things fail

- **401/403:** stop; correct credentials, account access, workspace policy or
  endpoint choice. Gateway `/v1` and direct Core use different credentials.
- **409 default workflow unavailable:** read the named setting/reason, discover
  valid bundles and correct the selection; no fallback is implied.
- **503 starting/restarting:** respect `Retry-After` where provided, inspect
  health, and distinguish a temporary restart from `model_host.state: failed`.
- **424 model host stopped:** inspect the run and incident; Gateway can remain
  healthy while the child recovers. After repeated crashes, correct the model
  or memory settings before restarting the Gateway.
- **Local artifact already resident with another profile:** reuse its compatible
  configuration or unload it before switching; a duplicate load is refused.

## Go deeper

- [Getting started](https://raw.githubusercontent.com/lpalbou/AbstractGateway/main/docs/getting-started.md): installation, workflow discovery and first run.
- [API](https://raw.githubusercontent.com/lpalbou/AbstractGateway/main/docs/api.md): view/delta shapes, commands, sessions and resources.
- [Configuration](https://raw.githubusercontent.com/lpalbou/AbstractGateway/main/docs/configuration.md): owner process, settings and split deployment.
- [Inference endpoints](https://raw.githubusercontent.com/lpalbou/AbstractGateway/main/docs/openai-api.md): direct Core versus Gateway `/v1` credentials.
- [Troubleshooting](https://raw.githubusercontent.com/lpalbou/AbstractGateway/main/docs/troubleshooting.md): failure diagnosis and recovery.
- [Full docs](https://raw.githubusercontent.com/lpalbou/AbstractGateway/main/llms-full.txt): expanded reference.
