# Automations API

An **automation** runs a workflow again and again on a trigger (for example
"every 2 minutes"), keeps every run as a readable conversation, and tells you
only when something needs your attention.

On the gateway an automation is a durable AbstractRuntime **root run** (its id
is the automation id), driven by a controller workflow that ships with
AbstractRuntime. Each time the trigger fires, the controller starts one
**occurrence**: an ordinary child run of the target workflow. The gateway only
projects that runtime state over HTTP; it never schedules or runs anything
itself.

Everything below lives under `/api/gateway` and needs the usual bearer token or
browser session ([security.md](./security.md)). Apps detect the API through
the capabilities document:

```json
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

## Errors

Every error on `/api/gateway/automations…` and `/api/gateway/trigger-sources`
has one shape, including sign-in failures and malformed requests:

```json
{"detail": {"reason_code": "revision_conflict", "message": "Automation is at revision 4, not 3.", "field": "expected_revision", "command_id": "…"}}
```

`field` and `command_id` appear when they apply.

| Status | `reason_code` |
|---|---|
| 401 | `unauthorized` (no or wrong token) |
| 403 | `forbidden` |
| 404 | `automation_not_found`, `occurrence_not_found` (another user's automation is also "not found") |
| 409 | `revision_conflict`, `automation_busy`, `invalid_state`, `identity_conflict` |
| 422 | `invalid_request` (including malformed JSON and unknown fields), `invalid_definition`, `unsupported_feature`, `unknown_trigger_source` |

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

`schedule@1` takes `{start_at?, every?, until?, count?}`: `every` is a fixed
interval such as `"2m"`, `"8h"` or `"1d"` (seconds, minutes, hours, days; UTC,
no calendar or daylight-saving rules), so interfaces say "every 8 hours",
never "daily at 08:00". `manual@1` takes `{}`: the automation runs only when
asked. A source installed by another package that fails to load is listed with
`"available": false` and an `unavailable_reason`; a missing built-in source is
an error (500 `internal_error`).

## Create an automation

`POST /api/gateway/automations`

```text
{
  "request_id": "0b6f…",               // your idempotency key: same request again = same automation
  "title": "Memory every 2 minutes",
  "target": {"bundle_ref": "memory-check@1.2.0", "flow_id": "check", "input_data": {"prompt": "Report memory use."}},
  "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "2m"}},
  "context": {"mode": "independent"},   // or "growing"
  "policy": {"retry": {"max_attempts": 3}}
}
→ {"automation_id": "…", "revision": 1, "summary": {…AutomationSummary}}
```

- `target` is a workflow this gateway serves: `{bundle_ref, flow_id}` (a
  version-less `bundle_ref` pins the version loaded now), or
  `{"flow_id": "@default", "interface": "abstractcode.agent.v1"}` for the
  gateway's default agent, resolved now. The concrete workflow is stored.
- `title` and `trigger` may be left out when the target publishes
  `automation_defaults` (below); the defaults then fill `title`, `trigger`,
  `context` and the inputs you did not send.
- `context.mode`: `independent` (default) starts every occurrence fresh;
  `growing` gives each occurrence the previous occurrences as conversation
  history (the last 40 messages, 24 000 characters at most).
- The automation works in its own folder under the gateway's data folder;
  the gateway's usual protection of the data folder and your credential
  folders applies to every occurrence.
- The same `request_id` with a different request → 409 `identity_conflict`.
- `policy.tool_approval`: `"auto"` (default) or `"ask"`. An automation runs
  unattended, so it cannot stop to ask before every tool call: **creating
  it is the consent** for the tools its target offers, and under `auto`
  every occurrence runs them without asking. `"ask"` keeps the approval
  wait of an ordinary chat on every tool batch (someone must answer each
  one). Questions the workflow itself asks (Ask User) wait for a person in
  both modes. Discussions always keep the interactive approval of ordinary
  chats.

## List, read, revise, command

| Route | Answer |
|---|---|
| `GET /api/gateway/automations?status=&cursor=&limit=` | `{items: [AutomationSummary], next_cursor}`, newest first. Read every page (`next_cursor` until `null`); `changed_since` is refused (422 `unsupported_feature`). |
| `GET /api/gateway/automations/{id}` | `{definition, active_revision, summary}` |
| `PATCH /api/gateway/automations/{id}` | `{command_id, expected_revision?, changes: {title?, target?, trigger?, context?, policy?}}` → `CommandReceipt` |
| `POST /api/gateway/automations/{id}/commands` | `{command_id, type, payload?}` → `CommandReceipt` |

`AutomationSummary`:

```json
{"automation_id": "…", "title": "…", "status": "active|paused|completed|failed|archived",
 "trigger": {"binding_id": "…", "source_id": "schedule", "source_version": 1, "config": {…}},
 "context_mode": "independent", "next_fire_at": "…", "occurrence_count": 6,
 "last_occurrence": {"run_id", "index", "status", "attempts", "fired_at", "finished_at", "excerpt", "notify"},
 "attention": {"pending_waits": 0, "unread": false, "unseen_count": 0, "cursor": "att1:3", "items": [], "waits": []},
 "legacy": false, "revision": 1, "updated_at": "…",
 "capabilities": ["revise", "pause", "resume", "run_now", "stop_current", "archive", "discuss"],
 "session_kind": "automation"}
```

A command is **accepted** when it is queued (`{command_id, accepted, duplicate, seq}`;
the same `command_id` again answers `duplicate: true` with the first `seq`). The
automation's history then records whether it was applied or rejected. The door
already refuses what it can check: a stale `expected_revision` (409
`revision_conflict`) and invalid `changes` (422).

| Type | Effect |
|---|---|
| `automation.pause` | no more scheduled runs; a running occurrence finishes; "run now" still works |
| `automation.resume` | continues at the next scheduled time; never fires on resume |
| `automation.run_now` | one run now, even while paused (it stays paused); rejected (`automation_busy`) while one is already running |
| `automation.revise` | `payload.changes`; used from the next run; a new schedule never fires a past tick |
| `automation.stop_current` | cancels the running occurrence |
| `automation.archive` | no more runs; history is kept |

## Occurrences

`GET /api/gateway/automations/{id}/occurrences?cursor=&limit=` → `{items, next_cursor}`,
newest first:

```text
{"run_id": "…", "index": 7, "attempts": 1, "fired_at": "…", "finished_at": "…",
 "status": "completed",          // admitted | running | waiting | backoff | completed | failed | cancelled
 "trigger": {"source_id": "schedule", "summary": "schedule: every 30 minutes (UTC), tick 5"},
 "user_turn": "[Trigger schedule@1 · occurrence 7 · fired …]\nTriage my inbox…",
 "answer": "…", "notify": null,
 "failure": {"reason_code": "occurrence_failed", "message": "…", "attempts": 3},   // failed rows only
 "artifacts": [{"artifact_id", "name", "mime_type", "url"}],
 "waits": [{"run_id", "wait_key", "reason", "prompt", "choices"}],
 "ledger_url": "/api/gateway/runs/…/ledger", "workspace_url": "/api/gateway/runs/…/workspace"}
```

`user_turn` and `answer` are the occurrence as a chat turn.

Each wait in `waits` (and in the summary's `attention.waits`) is typed:
`{run_id, wait_key, kind, reason, prompt?, choices?, details?}` with `kind`
one of `ask_user`, `tool_approval`, `event`; for `tool_approval`, `details`
lists the tool calls awaiting approval (`[{name, arguments, call_id?}]`).
Answer with the ordinary resume command on the wait's run,
`POST /api/gateway/commands` `{"type": "resume", "run_id": <wait run_id>,
"payload": {"wait_key": <wait_key>, "payload": <answer>}}`, where the answer
depends on `kind`:

| `kind` | Answer (`payload.payload`) |
|---|---|
| `ask_user` | `{"response": "…"}` |
| `tool_approval` | `{"approved": true}` or `{"approved": false}` (optional `tool_ids`) |
| `event` | the event payload (a JSON object) |

For an automation's runs the gateway refuses an answer of the wrong shape
(422 `invalid_request`, field `payload`): `{"response": "approve"}` to a tool
approval is refused, never recorded as the tool result.

### The notify convention

A workflow asks for attention through its OUTPUT: `notify: true` (the title
is the automation's title, the body the answer) or `notify: {"title": …, "body": …}`.
Anything else stays quiet: quiet occurrences are listed but raise no
notification. Failures (after the last retry) always raise one; a person-wait
is always counted in `attention.pending_waits`. A plain agent workflow that
must flag results is wrapped in a small flow that returns `{response, notify}`.

## Discuss an occurrence

`POST /api/gateway/automations/{id}/discuss` `{request_id, occurrence_index, prompt}` →
`{session_id, run_id, session_kind: "discussion"}`.

A discussion is a new conversation (its own session) that starts from the
automation's conversation up to that occurrence and runs the same workflow.
It can read the automation's folder but not change it: writes, edits and
command execution are refused. Continue it with `POST /api/gateway/runs/start`
and its `session_id`: the gateway marks every later turn the same way
(read-only folder, the discussion's history), whatever the request says.
Nothing a discussion does is written back into the automation.

## Older scheduled runs (legacy)

Schedules created with `POST /api/gateway/runs/schedule` are not converted.
They appear on the last page of `GET /api/gateway/automations` with
`legacy: true`, `revision: null` and `capabilities: ["legacy"]`, and keep
their old controls (`pause`, `resume`, `cancel` through `POST /api/gateway/commands`).
`GET /api/gateway/automations/{id}` answers 404 for them.

## Attention

An automation is quiet by default. An occurrence asks for attention only when
its output carries `notify: true` or `notify: {title, body}`, when it has
failed after its last retry, or while it waits for a person. Attention items
live in the automation's own run history; each user's "seen" position is kept
by the gateway per user (`<data dir>/automations/attention/`).

`GET /api/gateway/automations/{automation_id}/attention?cursor=&limit=` pages
this user's UNSEEN items, oldest first (`{kind: "notify"|"failure",
automation_id, run_id, index, at, title, body?, cursor}`).

`POST /api/gateway/automations/{automation_id}/seen` with
`{"attention_cursor": "att1:<n>"}` records what this user has seen and returns
the stored cursor. It only moves forward: an older cursor is ignored and the
newer stored one is returned. Send the cursor of the last item you actually
displayed, never a newer one, so items you did not show stay unseen. A cursor
beyond the automation's latest item is refused (422 `invalid_request`). Waiting
occurrences stay counted until they are answered; `seen` does not clear them.

## Automations in run lists

`GET /api/gateway/runs` rows carry four fields that say how a run belongs to an
automation:

| Field | Values |
|---|---|
| `session_kind` | `chat`, `automation`, `occurrence`, `discussion` |
| `automation_id` | the automation the run belongs to, or `null` |
| `role` | `controller`, `occurrence`, `descendant`, `discussion`, `legacy_schedule`, or `null` |
| `occurrence_index` | the occurrence number, or `null` |

- `session_kind=chat,discussion` filters the list (a comma-separated subset).
  A normal chat list uses `root_only=true&session_kind=chat,discussion`.
- `root_only=true` returns **turns**: runs without a parent, except automation
  controllers, plus occurrences. An automation that keeps a growing
  conversation therefore reads as one chat session, one turn per occurrence,
  with no client changes. `GET /api/gateway/sessions/{session_id}/history/bloc`
  returns the same turns (a retried occurrence counts once, as its last
  attempt).
- Every row also has `legacy`: `true` for an older scheduled run
  (`POST /api/gateway/runs/schedule`), which lists with
  `role: "legacy_schedule"`, `session_kind: "automation"` and
  `is_scheduled: true`, so a chat list never shows it as a chat.

## Automation defaults on workflows

A workflow can suggest how automations created from it should run. The flow
editor stores the suggestion in the VisualFlow document as
`automation_defaults`:

```json
"automation_defaults": {
  "schema_version": 1,
  "title": "Memory every 2 minutes",
  "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "2m"}},
  "context": {"mode": "independent"},
  "input_data": {"prompt": "Report the memory usage of this computer."}
}
```

- `POST /api/gateway/visualflows` and `PUT /api/gateway/visualflows/{id}`
  accept it; saving checks it (unknown fields, a `binding_id` or a trigger
  config the source refuses → 422 with `reason_code` and `field`). `context`
  defaults to `{"mode": "independent"}`, `input_data` to `{}`.
- `PUT` with `"automation_defaults": null` removes it; leaving the field out
  keeps it. `PUT` and `GET` return the stored value.
- Publishing writes it into the bundle manifest as
  `metadata.automation_defaults[<flow id>]`. `GET /api/gateway/bundles`,
  `GET /api/gateway/bundles/{bundle_id}` and the shared catalog records return
  `automation_defaults` keyed by entrypoint flow id.

## Commands through the run command door

The six `automation.*` command types also go through the durable command door
of run commands, `POST /api/gateway/commands`, with `run_id` set to the
automation id. The door refuses (404) an `automation.*` command at a run that
is not an automation. The runner hands each command to AbstractRuntime, which
records whether it was applied or rejected in the automation's history.

Evidence: `src/abstractgateway/routes/automations.py`,
`src/abstractgateway/automation_errors.py`,
`src/abstractgateway/automation_attention.py`,
`src/abstractgateway/automation_defaults.py`,
`src/abstractgateway/automation_command_types.py`; the automation logic
itself is AbstractRuntime's (`abstractruntime.automations`). A runnable
end-to-end check: `python scripts/accept_automations_v1.py --data-dir <empty folder>`.
