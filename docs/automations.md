# Automations

An **automation** runs a workflow again and again on a trigger ("every 2
minutes", or only when you ask), keeps every run as a readable chat turn, and
tells you only when something needs your attention. This page is the HTTP
contract for automations on AbstractGateway: every route with its request and
response shapes, the errors, how waits are answered, and how to operate
automations on a gateway.

The automation logic itself (controller, triggers, retries, context modes,
discussions, read-only workspaces) is AbstractRuntime's and is described in
AbstractRuntime's `docs/automations.md`. For the gateway's components see
[architecture.md](./architecture.md#automations); for the rest of the HTTP API
see [api.md](./api.md).

## How automations work

An automation is one durable AbstractRuntime **root run**, its **controller**,
whose run id is the automation id. The controller is a workflow that ships
with AbstractRuntime; the gateway's runner ticks it like any other run. Each
time the trigger fires, the controller starts one **occurrence**: an ordinary
child run of your target workflow, with the inputs frozen at creation. An
occurrence reads as a chat turn (the prompt it received and its answer). In
**independent** mode every occurrence starts fresh; in **growing** mode each
occurrence sees the previous ones as conversation history. Automations are
**quiet**: an occurrence raises attention only when its output asks for it,
when it fails after its last retry, or while it waits for a person. The
gateway projects this runtime state over HTTP and queues your commands; it
never schedules or runs anything itself.

## Before you start

- **Sign-in.** Every route lives under `/api/gateway` and needs the usual
  bearer token or browser session ([security.md](./security.md)). Each user
  sees only their own automations: another user's automation answers 404.
- **Detection.** Apps gate their automation UI on the capabilities document:

  ```text
  GET /api/gateway/discovery/capabilities
  → capabilities.contracts.common.automations = {
      "available": true, "version": 1,
      "endpoint": "/api/gateway/automations",
      "automations_endpoint": "/api/gateway/automations",
      "trigger_sources_endpoint": "/api/gateway/trigger-sources",
      "command_types": ["automation.revise", "automation.pause", "automation.resume",
                        "automation.run_now", "automation.stop_current", "automation.archive"]
    }
  ```

- **Runtime.** Automations need AbstractRuntime with automations support (the
  next release after 0.5.1). A gateway installed against an older runtime does
  not start ([troubleshooting.md](./troubleshooting.md#the-gateway-does-not-start-an-abstractruntime-module-is-missing)).

The examples below assume:

```bash
export BASE_URL="http://127.0.0.1:8080"
export AUTH="Authorization: Bearer $(cat "$ABSTRACTGATEWAY_DATA_DIR/auth/bootstrap-admin-token")"
```

## Routes at a glance

| Route | Purpose |
|---|---|
| `GET /api/gateway/trigger-sources` | what can start an automation ([Trigger sources](#trigger-sources)) |
| `POST /api/gateway/automations` | create an automation ([Create](#create-an-automation)) |
| `GET /api/gateway/automations` | list your automations, older scheduled runs included ([List and read](#list-and-read)) |
| `GET /api/gateway/automations/{automation_id}` | one automation: definition and summary |
| `PATCH /api/gateway/automations/{automation_id}` | revise title, target, trigger, context or policy ([Change](#change-an-automation)) |
| `POST /api/gateway/automations/{automation_id}/commands` | pause, resume, run now, stop the current run, archive |
| `GET /api/gateway/automations/{automation_id}/occurrences` | every run of the automation as a chat turn ([Occurrences](#occurrences)) |
| `GET /api/gateway/automations/{automation_id}/attention` | the notifications you have not seen ([Attention](#attention-and-seen)) |
| `POST /api/gateway/automations/{automation_id}/seen` | record what you have seen |
| `POST /api/gateway/automations/{automation_id}/discuss` | open a separate conversation about one occurrence ([Discuss](#discuss-an-occurrence)) |
| `POST /api/gateway/commands` | answer an occurrence's wait ([Waits](#waits-on-a-person)); also accepts the `automation.*` commands |
| `GET /api/gateway/runs` | run lists with automation attribution ([Run lists](#automations-in-run-lists)) |

## Errors

Every error on `/api/gateway/automations…` and `/api/gateway/trigger-sources`
has one shape, including sign-in failures, request-validation failures and
malformed JSON:

```json
{"detail": {"reason_code": "revision_conflict", "message": "Automation is at revision 4, not 3.", "field": "expected_revision", "command_id": "c-17"}}
```

`field` names the offending input and `command_id` echoes the command, when
they apply.

| Status | `reason_code` | When |
|---|---|---|
| 401 | `unauthorized` | no token, a wrong token or an expired session |
| 403 | `forbidden` | the security layer refused the request (for example an origin outside the allowlist, or a browser-session write without its CSRF token) |
| 404 | `automation_not_found` | no automation with this id for you (another user's automation included) |
| 404 | `occurrence_not_found` | the automation has no occurrence with this index |
| 409 | `revision_conflict` | `expected_revision` is not the automation's revision |
| 409 | `automation_busy` | `run_now` while an occurrence is running or waiting to run |
| 409 | `invalid_state` | the automation's state rules the command out (see [Commands](#commands)) |
| 409 | `identity_conflict` | a `request_id` or `command_id` reused for a different request |
| 409 | `cursor_expired`, `history_unavailable` | a list cursor that has expired; a conversation history that cannot be read |
| 422 | `invalid_request` | a missing, unknown or malformed field (including malformed JSON) |
| 422 | `invalid_definition` | a target, trigger, context or policy the gateway or runtime refuses |
| 422 | `unsupported_feature` | a valid request that v1 does not support (for example `changed_since`, or a policy value other than the fixed v1 value) |
| 422 | `unknown_trigger_source` | a trigger `source_id`/`source_version` no installed source provides |

Rare transport answers keep the same shape with `not_found` (unknown path),
`rate_limited` (429), `unavailable` (503) or `internal_error` (500).

## Trigger sources

`GET /api/gateway/trigger-sources` lists what can start an automation:

```json
{"items": [
  {"id": "schedule", "version": 1, "label": "Schedule", "capabilities": {"kind": "time"},
   "config_schema": {…}, "event_schema": {…}, "available": true},
  {"id": "manual", "version": 1, "label": "Manual", "capabilities": {"kind": "manual"}, …, "available": true}
]}
```

- **`schedule@1`** takes `{start_at?, every?, until?, count?}`. `every` is a
  fixed interval: a number and one of `s`, `m`, `h`, `d` (`"2m"`, `"8h"`,
  `"1d"`), at most `366d`. Intervals are plain UTC durations with no calendar
  or daylight-saving rules, so interfaces say "every 8 hours", never "daily
  at 08:00". `start_at` defaults to the creation time and anchors the grid;
  `until` is exclusive and must be after `start_at`; `count` (1 to 1,000,000)
  caps the number of ticks and needs `every` when above 1. Without `every`
  the schedule fires once, at `start_at`. Ticks missed while the gateway was
  down coalesce into one occurrence.
- **`manual@1`** takes `{}`: the automation runs only when you send
  `automation.run_now`.

A source installed by another package that fails to load is listed with
`"available": false` and an `unavailable_reason`. A missing built-in source is
an error (500 `internal_error`).

## Create an automation

`POST /api/gateway/automations`

```text
{
  "request_id": "0b6f…",               // your idempotency key
  "title": "Memory every 2 minutes",
  "target": {"bundle_ref": "memory-check@1.2.0", "flow_id": "check",
             "input_data": {"prompt": "Report the memory use of this computer."}},
  "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "2m"}},
  "context": {"mode": "independent"},   // or "growing"
  "policy": {"tool_approval": "auto", "retry": {"max_attempts": 3}}
}
→ {"automation_id": "…", "revision": 1, "summary": {…AutomationSummary}}
```

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"request_id":"'"$(python -c 'import uuid; print(uuid.uuid4())')"'","title":"Memory every 2 minutes","target":{"flow_id":"@default","interface":"abstractcode.agent.v1","input_data":{"prompt":"Report the memory use of this computer."}},"trigger":{"source_id":"schedule","source_version":1,"config":{"every":"2m"}}}' \
  "$BASE_URL/api/gateway/automations"
```

**`request_id`.** The automation id is derived from your user and the
`request_id`, so sending the same request again returns the same automation.
The same `request_id` with a different request answers 409
`identity_conflict`.

**`target`.** A workflow this gateway serves, in one of two forms:

- `{bundle_ref, flow_id, input_data?}`: `bundle_ref` is `bundle_id` or
  `bundle_id@version`; a version-less reference pins the version loaded at
  creation. `flow_id` must be one of the bundle's entrypoints.
- `{"flow_id": "@default", "interface": "<agent interface>", "input_data"?}`:
  the gateway default workflow of that agent interface (for example
  `abstractcode.agent.v1`), resolved at creation
  ([configuration.md](./configuration.md#default-agent-workflow)). Do not send
  `bundle_ref` with it. A default that points into the shared workflow catalog
  is refused (422 `unsupported_feature`): automations run workflows of the
  gateway's own registry.

The concrete workflow (`bundle_id@version:flow_id`) is stored in the
definition, so later changes to the default do not move the automation.

**`target.input_data`** is your workflow's input. The gateway applies the
same protections as for any run: your workspace settings are clamped to the
operator's workspace policy, the automation gets its own folder under the
gateway's data folder, and the built-in deny rules keep its tools away from
the data folder and your credential folders ([security.md](./security.md)).
Keys the server owns are dropped: `_meta`, `workspace_read_only`, and every
`_runtime` key except these, which a client may set:

| `_runtime` key | What it sets |
|---|---|
| `allowed_tools` | narrows the tools the target may call |
| `provider`, `model` | the model the target uses |
| `thinking`, `speculation` | generation settings, as in a chat run |
| `stream` | live token streaming (`true`/`false`) |

Anything else under `_runtime` (for example `control`, `tool_policy`,
`agora_agent`) is runtime or host state and is dropped. Tool approval is set
by `policy.tool_approval`. A workflow's `automation_defaults` may not carry
server-owned keys at all: saving the workflow answers 422 with the `field`.

**`context.mode`.**

| Mode | Each occurrence | Session |
|---|---|---|
| `independent` (default) | starts fresh, with no history | a new session per occurrence |
| `growing` | receives the previous occurrences as conversation history: whole turns, at most 40 messages and 24,000 characters, not summarized | the automation's session, `automation:<automation_id>` |

**`policy`.** `tool_approval` is `"auto"` (default) or `"ask"`
([Tool approval](#tool-approval-and-consent)). `retry` sets how a failed
occurrence is retried: `max_attempts` 1 to 10 (default 3) and `backoff`
`{initial: "30s", factor: 2, max: "10m"}` by default. `serial: true`,
`misfire: "coalesce"` and `failure: "continue"` are the only v1 values;
anything else answers 422 `unsupported_feature`.

**`title` and `trigger` may be left out** when the target publishes
`automation_defaults` ([below](#automation-defaults-on-workflows)): the
defaults then fill `title`, `trigger`, `context`, and the `input_data` keys
you did not send. Without published defaults, a missing `title` or `trigger`
answers 422 `invalid_request`.

### Automation defaults on workflows

A workflow can suggest how automations created from it should run. The flow
editor stores the suggestion in the VisualFlow document as
`automation_defaults`:

```json
"automation_defaults": {
  "schema_version": 1,
  "title": "Memory every 2 minutes",
  "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "2m"}},
  "context": {"mode": "independent"},
  "input_data": {"prompt": "Report the memory use of this computer."}
}
```

- `POST /api/gateway/visualflows` and `PUT /api/gateway/visualflows/{id}`
  accept it and check it on save: unknown fields, a `binding_id`, a title over
  120 characters, or a trigger config its source refuses answer 422 with
  `reason_code` and `field`. `context` defaults to `{"mode": "independent"}`
  and `input_data` to `{}`.
- `PUT` with `"automation_defaults": null` removes it; leaving the field out
  keeps it. `PUT` and `GET` return the stored value.
- Publishing writes it into the bundle manifest as
  `metadata.automation_defaults[<flow id>]`. `GET /api/gateway/bundles`,
  `GET /api/gateway/bundles/{bundle_id}` and the shared catalog records return
  `automation_defaults` keyed by entrypoint flow id.
- At creation the defaults are checked again with the same rules; invalid
  published defaults answer 422 with the offending `field`.

## Tool approval and consent

An automation runs unattended, so it cannot stop to ask before every tool call.
`policy.tool_approval` decides what happens:

- **`"auto"` (default).** Creating the automation is the consent for the tools
  its target offers: every occurrence runs them without asking. Apps state
  this on their creation form and list the tools. The grant is limited to the
  target's `_runtime.allowed_tools` when it has that list, and to the tools
  AbstractRuntime classifies otherwise; a tool outside that classification
  (a third-party MCP tool, for example) still asks.
- **`"ask"`.** Every tool batch that needs approval waits on a
  `tool_approval` wait, as in an ordinary chat, and someone must answer it
  ([Waits](#waits-on-a-person)).

Questions the workflow itself asks (Ask User) wait for a person in both modes.
A change of `tool_approval` applies from the next occurrence. Discussions
never inherit the grant: their tools ask as in any chat.

## List and read

| Route | Answer |
|---|---|
| `GET /api/gateway/automations?status=&cursor=&limit=` | `{items: [AutomationSummary], next_cursor}`, newest first |
| `GET /api/gateway/automations/{automation_id}` | `{definition, active_revision, summary: AutomationSummary}` |

- `status` is a comma-separated subset of `active`, `paused`, `completed`,
  `failed`, `archived`. `limit` is 1 to 200 (default 50).
- Read every page, following `next_cursor` until it is `null`: older
  scheduled runs ([Legacy](#older-scheduled-runs-legacy)) come on the last
  page. `changed_since` is refused (422 `unsupported_feature`); poll complete
  pages instead.

`AutomationSummary`:

```text
{"automation_id": "…", "title": "…",
 "status": "active",                  // active | paused | completed | failed | archived
 "trigger": {"binding_id": "…", "source_id": "schedule", "source_version": 1, "config": {…}},
 "context_mode": "independent", "next_fire_at": "…", "occurrence_count": 6,
 "last_occurrence": {"run_id", "index", "status", "attempts", "fired_at", "finished_at"?, "excerpt", "notify"},
 "attention": {"pending_waits": 0, "unread": false, "unseen_count": 0, "cursor": "att1:3", "items": [], "waits": []},
 "legacy": false, "revision": 1, "updated_at": "…",
 "capabilities": ["revise", "pause", "resume", "run_now", "stop_current", "archive", "discuss"],
 "session_kind": "automation"}
```

- `next_fire_at` is present only while a tick is scheduled.
- `last_occurrence.excerpt` holds the first 280 characters of its answer.
- `attention` is described in [Attention](#attention-and-seen); `items` and
  `waits` hold up to 20 entries each.
- `capabilities` lists what the automation accepts. An archived, completed or
  failed automation lists `["discuss"]` only.

`definition` is the stored definition (target, trigger, context, policy,
revision); `active_revision` is the revision in force.

## Change an automation

### Revise

`PATCH /api/gateway/automations/{automation_id}`

```text
{"command_id": "…", "expected_revision": 3, "changes": {"title"?, "target"?, "trigger"?, "context"?, "policy"?}}
→ CommandReceipt
```

- `changes` must be a non-empty object with only those keys. A `target` is
  resolved and protected as at creation. A `policy` change is merged into the
  current policy, so a retry-only change keeps `tool_approval`.
- A revision applies from the next occurrence; the running one finishes under
  the old definition. A new schedule never fires a past tick.
- `expected_revision` is optional; a stale value answers 409
  `revision_conflict`. Invalid `changes` answer 422 before anything is queued.

### Commands

`POST /api/gateway/automations/{automation_id}/commands`

```text
{"command_id": "…", "type": "automation.pause", "payload": {}}
→ {"command_id": "…", "accepted": true, "duplicate": false, "seq": 42}
```

| Type | Effect |
|---|---|
| `automation.pause` | no more scheduled occurrences; a running one finishes; "run now" still works |
| `automation.resume` | continues at the next scheduled time; resuming never fires at once |
| `automation.run_now` | one occurrence now, also while paused (the automation stays paused) |
| `automation.revise` | `payload: {changes, expected_revision?}`; the same as `PATCH` |
| `automation.stop_current` | cancels the running occurrence |
| `automation.archive` | no more occurrences; the history is kept |

**The receipt means queued.** A command is `accepted` when it is written to
the gateway's durable command inbox. The runner hands it to AbstractRuntime,
which records in the automation's ledger whether it was applied or rejected
(`GET /api/gateway/runs/{automation_id}/ledger`). The same `command_id` again
answers `duplicate: true` with the first `seq`; the same `command_id` for a
different command answers 409 `identity_conflict`.

**The route refuses at once what the state already rules out** (409):

- any command but `archive` on an archived, completed or failed automation
  (`invalid_state`);
- `run_now` while an occurrence is running or waiting to run
  (`automation_busy`), or when the trigger has no ticks left (`invalid_state`);
- `pause` when paused, `resume` when not paused, `stop_current` with nothing
  running (`invalid_state`);
- a stale `payload.expected_revision` (`revision_conflict`).

AbstractRuntime checks again when it applies the command, because the state
may change in between.

**Through the run command door.** `POST /api/gateway/commands` also accepts
the six `automation.*` types with `run_id` set to the automation id; it
answers 404 when `run_id` is not an automation. Prefer the automation routes
above, which also refuse invalid states and revisions at once. Run commands
(`pause`, `resume`, `cancel`, `conclude`, `update_schedule`, …) aimed at an
automation id answer 409 `invalid_state`: an automation is controlled only
with the `automation.*` types.

## Occurrences

`GET /api/gateway/automations/{automation_id}/occurrences?cursor=&limit=` →
`{items, next_cursor}`, newest first, one row per occurrence (its latest
attempt):

```text
{"run_id": "…", "index": 7, "attempts": 1, "fired_at": "…", "finished_at": "…",
 "status": "completed",   // admitted | running | waiting | backoff | completed | failed | cancelled
 "trigger": {"source_id": "schedule", "summary": "schedule: every 30 minutes (UTC), tick 5"},
 "user_turn": "[Trigger schedule@1 · occurrence 7 · fired …]\nTriage my inbox…",
 "answer": "…", "notify": null,
 "failure": {"reason_code": "occurrence_failed", "message": "…", "attempts": 3},   // failed rows only
 "artifacts": [{"artifact_id", "name", "mime_type", "url"}],
 "waits": [{"run_id", "wait_key", "kind", "reason", "prompt"?, "choices"?, "details"?}],
 "ledger_url": "/api/gateway/runs/…/ledger", "workspace_url": "/api/gateway/runs/…/workspace"}
```

- `user_turn` and `answer` are the occurrence as a chat turn; `answer` is
  empty until the occurrence ends.
- `status` is `waiting` only while the occurrence, or a run below it, waits
  for a person (it then has `waits`). An occurrence whose agent is still
  working reads `running`. `backoff` means a retry is scheduled.
- `trigger.summary` describes the trigger the occurrence ran under, also
  after a revision; a manual occurrence reads `manual: run now (<command id>)`.
- `artifacts[].url` serves the file; `workspace_url` browses the occurrence's
  folder ([api.md](./api.md#a-runs-workspace-folder-browse-and-preview)).

## Waits on a person

An occurrence (or a run below it) can wait for a person. Each wait, in an
occurrence row's `waits` and in the summary's `attention.waits` (which also
carries the occurrence `index`), is typed:

```text
{"run_id": "…", "wait_key": "…", "kind": "ask_user" | "tool_approval" | "event",
 "reason": "…", "prompt"?: "…", "choices"?: […], "details"?: …}
```

For `tool_approval`, `details` lists the tool calls that approving will run:
`[{name, arguments, call_id?}]`.

Answer a wait with the ordinary resume command on the wait's run:

```bash
curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"command_id":"'"$(python -c 'import uuid; print(uuid.uuid4())')"'","run_id":"<wait run_id>","type":"resume","payload":{"wait_key":"<wait_key>","payload":{"approved":true}}}' \
  "$BASE_URL/api/gateway/commands"
```

The answer, `payload.payload`, depends on `kind`:

| `kind` | What waits | Answer (`payload.payload`) |
|---|---|---|
| `ask_user` | a question the workflow asks | `{"response": "…"}` |
| `tool_approval` | a tool batch awaiting approval | `{"approved": true}` or `{"approved": false}`, optionally with `"tool_ids": ["…"]` |
| `event` | an event wait that carries a prompt or choices | `{"payload": {…}}` (the event payload, wrapped) |

For an automation's runs the gateway refuses an answer of the wrong shape
(422 `invalid_request`, field `payload`, with the expected shape in the
message): `{"response": "approve"}` to a tool approval is refused, never
recorded as the tool result. Waiting occurrences stay counted in
`attention.pending_waits` until they are answered.

## Attention and `/seen`

### What asks for attention

A workflow asks for attention through its **output**:

- `notify: true`: the title is the automation's title and the body the
  answer;
- `notify: {"title": "…", "body": "…"}`: your own title and body.

Anything else stays quiet: quiet occurrences are listed but raise no
notification. An occurrence that fails after its last retry always raises
one. A plain agent workflow that must flag its results is wrapped in a small
flow that returns `{response, notify}`.

### Reading and marking attention

Attention items live in the automation's own history; what each user has
**seen** is kept by the gateway per user, in
`<data dir>/automations/attention/`.

- The summary's `attention` block: `unseen_count` and `unread` (items you
  have not seen), `items` (up to 20 of them), `cursor` (the latest item's
  cursor), `pending_waits` and `waits` (live waits, up to 20).
- `GET /api/gateway/automations/{automation_id}/attention?cursor=&limit=`
  pages your unseen items, oldest first: `{items, next_cursor}` with items
  `{kind: "notify" | "failure", automation_id, run_id, index, at, title, body?, cursor}`.
- `POST /api/gateway/automations/{automation_id}/seen` with
  `{"attention_cursor": "att1:<n>"}` records what you have seen and returns
  `{"attention_cursor": "att1:<stored n>"}`.

`/seen` only moves forward: an older cursor is ignored and the stored one is
returned. Send the cursor of the last item you actually displayed, never the
summary's latest `cursor`, so items you did not show stay unseen. A cursor
beyond the automation's latest item, or one that does not match `att1:<n>`,
answers 422 `invalid_request`. `/seen` never clears waits: they stay counted
until they are answered.

## Discuss an occurrence

`POST /api/gateway/automations/{automation_id}/discuss`

```text
{"request_id": "…", "occurrence_index": 7, "prompt": "Why did memory jump here?"}
→ {"session_id": "discussion-session:…", "run_id": "…", "session_kind": "discussion"}
```

A discussion is a separate conversation about one occurrence. It runs the
occurrence's workflow with its frozen inputs, your `prompt` as the new user
turn, and the conversation up to and including that occurrence as its history
(the automation's session in growing mode, the occurrence's own session in
independent mode).

- **Identity.** The discussion's run id is
  `uuid5(automation_id, "discuss:" + request_id)` and its session id is
  `discussion-session:<that run id>`, so request ids are scoped to the
  automation. The same request returns the same discussion; a different
  request with the same `request_id` on that automation answers 409
  `identity_conflict`.
- **Read-only folder.** The discussion can read the occurrence's folder but
  not change it: writes, edits and command execution are refused.
- **Nothing flows back.** Nothing a discussion does is written into the
  automation's conversation, state or history.
- **Tools ask.** The automation's tool grant does not apply; tools ask for
  approval as in any chat.
- **Errors.** 404 `occurrence_not_found` for an unknown index, 422
  `invalid_request` for an empty `prompt` or `request_id`, 409
  `history_unavailable` when the history cannot be read.

**Continue a discussion** with `POST /api/gateway/runs/start` and its
`session_id`. The gateway re-stamps every later turn from the discussion's
first run, whatever the request says: the same provenance, the occurrence's
folder mounted read-only, and the discussion's history. The history is
provided by the gateway, so a request that sends `context.messages` in a
discussion session is refused (400); a session whose discussion cannot be
resolved answers 409 `session_attribution_failed`. These answers come from
`/runs/start` and use its ordinary error shape.

## Automations in run lists

`GET /api/gateway/runs` rows carry the fields that say how a run belongs to an
automation:

| Field | Values |
|---|---|
| `session_kind` | `chat`, `automation`, `occurrence`, `discussion` |
| `automation_id` | the automation the run belongs to, or `null` |
| `role` | `controller`, `occurrence`, `descendant`, `discussion`, `legacy_schedule`, or `null` |
| `occurrence_index` | the occurrence number, or `null` |
| `legacy` | `true` for an older scheduled run |

A growing automation's occurrences have `session_kind: "automation"`; an
independent automation's occurrences have `session_kind: "occurrence"`.

- **`session_kind` filter.** A comma-separated subset, for example
  `session_kind=chat,discussion`. An unknown kind answers 400.
- **`root_only=true` returns turns:** runs without a parent, except automation
  controllers, plus occurrences. A retried occurrence is one turn (its last
  attempt). A growing automation therefore reads as one chat session with one
  turn per occurrence, with no client changes.
- **A normal chat list** uses `root_only=true&session_kind=chat,discussion`.
- `GET /api/gateway/sessions/{session_id}/history/bloc` returns the same turns
  ([api.md](./api.md#session-history-bloc-get-sessionssession_idhistorybloc)).
- An older scheduled run lists with `role: "legacy_schedule"`,
  `session_kind: "automation"`, `is_scheduled: true` and `legacy: true`, so a
  chat list never shows it as a chat.

## Older scheduled runs (legacy)

Schedules created with `POST /api/gateway/runs/schedule`
([api.md](./api.md#2b-schedule-a-run-bundle-mode)) are not converted. They
appear on the last page of `GET /api/gateway/automations` with `legacy: true`,
`revision: null` and `capabilities: ["legacy"]`, and keep their own controls
(`pause`, `resume`, `cancel` through `POST /api/gateway/commands`).

- `GET /api/gateway/automations/{id}` and the other per-automation routes
  answer 404 for them.
- `trigger.binding_id` is the schedule's root run id; `last_occurrence` is its
  latest child run, with `index` its position among the children.

## Operations

**One writer process per data folder.** v1 supports one process that ticks
runs, resumes them and applies commands on a data folder: the runner. Run
either `abstractgateway serve` (API and runner in one process) or the split
shape, `abstractgateway runner` plus `abstractgateway serve --no-runner`, on
one data folder ([getting-started.md](./getting-started.md#3-split-api-vs-runner-recommended-for-upgrades)).
In the split shape the API process only creates automations and discussions
and queues commands; the runner does the rest. The lock that serializes an
automation's steps and commands works inside one process, so do not point a
second gateway at the same data folder.

**The runner drives controllers.** Every workflow host registers the
controller workflow that ships with AbstractRuntime,
`abstractframework.automation-controller@1.0.0:controller` (it is not a bundle
and does not appear in bundle lists), so the runner ticks controllers like any
run. A controller sleeps on one wait until its next tick or retry; a command
wakes it for an immediate tick. Nothing fires while no runner is ticking the
data folder: `GET /api/health` reports the runner (`runner.runners[].status`).

**Restart recovery.** Automations are durable runs, so they survive a
restart:

- After a restart the runner picks up every controller where it stopped.
- Occurrence ids are deterministic and a child run is created only when its id
  is free, so a restart in the middle of an occurrence continues that
  occurrence and never starts it twice.
- Commands accepted before the restart are applied from the runner's saved
  command position; a replayed command is recognized by its `command_id`.
- Ticks missed while the gateway was down coalesce into one occurrence.
- A command that fails on the host is recorded as rejected
  (`internal_error`); if even that record cannot be written, the runner keeps
  the command and retries it at the next poll.

**Boot warm-up.** With file-backed stores the gateway builds the run store's
session and children indexes when it starts, so the first chat does not pay
for that scan. The log line
`run store session index warmed in <seconds>s` reports the time (about a
second for 20,000 runs). SQLite stores keep these indexes in the database.

**Acceptance check.** From a source checkout,
`scripts/accept_automations_v1.py` checks the whole feature end to end
against a gateway it starts and stops itself:

```bash
python scripts/accept_automations_v1.py --data-dir /tmp/automation-acceptance
```

- `--data-dir` must be an empty or new folder; the script owns it.
- `--interval` (default `20s`) is the schedule of its test monitors, short so
  the run is quick; `--port` picks the port (default: the first free port from
  18900).
- It needs no model provider: its workflows are deterministic, and a local
  spy server checks that no provider call is made.
- It prints one `PASS` or `FAIL` line per step (creation, scheduled ticks,
  quiet versus notable occurrences, pause, run now while paused, resume
  without firing, revise, independent versus growing context, a restart in
  the middle of an occurrence, a two-turn discussion with refused writes,
  attention items, an unattended tool call, tool approval answered by kind,
  and a replay that makes zero calls) and exits 0 only when every step passes.

## Limits in v1

- Schedules are fixed UTC intervals (`s`, `m`, `h`, `d`, at most `366d`):
  no cron expressions, calendar months, time zones or daylight-saving rules.
- The trigger sources are `schedule@1` and `manual@1`; there are no external
  or event triggers.
- Occurrences run one at a time, missed ticks coalesce, and a failed
  occurrence never stops the automation. These policies cannot be changed.
- Growing history is bounded (40 messages, 24,000 characters, whole turns)
  and is not summarized.
- Under `tool_approval: "auto"`, tools outside AbstractRuntime's
  classification, such as third-party MCP tools, still ask for approval.
- Retries repeat the external effects of the failed attempt.
- A `@default` target must resolve to a workflow of the gateway's own
  registry, not the shared catalog.
- `GET /api/gateway/automations` has no change cursor: poll complete pages.
- The summary's `attention.items` and `attention.waits` hold up to 20 entries.
- Older scheduled runs are listed read-only and never converted.
- One writer process per data folder.

## Related docs

- [api.md](./api.md): the rest of the HTTP contract, durable commands and run lists
- [architecture.md](./architecture.md#automations): where automations sit in the gateway
- [security.md](./security.md): sign-in, the workspace guard and the built-in deny list
- [faq.md](./faq.md#automations) and [troubleshooting.md](./troubleshooting.md#automations)
- AbstractRuntime `docs/automations.md`: the controller, triggers, retries, context modes and discussions
