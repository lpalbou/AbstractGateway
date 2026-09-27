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

## Attention

An automation is quiet by default. An occurrence asks for attention only when
its output carries `notify: true` or `notify: {title, body}`, when it has
failed after its last retry, or while it waits for a person. Attention items
live in the automation's own run history; each user's "seen" position is kept
by the gateway per user (`<data dir>/automations/attention/`).

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

## Commands

The six `automation.*` command types go through the same durable command door
as run commands, `POST /api/gateway/commands`, with `run_id` set to the
automation id. The door refuses (404) an `automation.*` command at a run that
is not an automation. The runner hands each command to AbstractRuntime, which
records whether it was applied or rejected in the automation's history.

Evidence: `src/abstractgateway/routes/automations.py`,
`src/abstractgateway/automation_errors.py`,
`src/abstractgateway/automation_attention.py`,
`src/abstractgateway/automation_defaults.py`,
`src/abstractgateway/automation_command_types.py`.
