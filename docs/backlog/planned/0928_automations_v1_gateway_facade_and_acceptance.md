# 0928 — Automations v1: `/automations` façade over runtime objects, `automation.*` commands on the existing command store, session_kind projection, attention cursors, legacy schedule projection, catalog automation_defaults, acceptance script

**Status**: planned · **Priority**: P1 · **Created**: 2026-09-26
**Package**: abstractgateway · **Related**: abstractframework backlog 0928 (master item),
abstractruntime Automations v1 item (runtime objects, controller, queries — lands first),
abstractgateway 0930 (console inventory, phase after v1), abstractgateway 0929 (v2 external events)
**Design**: untracked/design/automations-PLAN.md (2026-09-26)

## Summary

An Automation is a durable runtime root run (the controller) whose scheduled or manual
occurrences are serial child runs. AbstractRuntime owns the definition/state/ledger
(contract A), attribution and creation (B), trigger sources (C), the controller bundle (D)
and the queries (E). The gateway slice is contract F: a thin HTTP façade that PROJECTS
runtime truth — it never schedules or executes work itself. Concretely:

- `/automations` routes (create, list, get, patch, commands, occurrences, discuss, seen) and
  `GET /trigger-sources`, all backed by runtime objects and queries.
- `automation.*` command types accepted on the existing durable command store and applied
  by the runner through the runtime's `apply_automation_command()`.
- `session_kind` (`chat|automation|occurrence|discussion`) projected on run and session
  listings, so normal lists show `chat` and `discussion` and the Automations view groups by
  `automation_id`.
- Per-principal monotonic attention cursors (`POST /automations/{id}/seen`).
- Legacy `POST /runs/schedule` schedules projected as `legacy:true` summaries with their
  existing controls, unchanged in behaviour.
- Catalog defaults read from `manifest.metadata.automation_defaults[flow_id]`.
- `capabilities` advertising the Automation API so apps can gate on it.
- A runnable end-to-end acceptance script, `scripts/accept_automations_v1.py`.

## Why

Operator constraints (PLAN §1, verbatim):

- **Runtime-native.** Automations execute as durable runtime workflows, with ordinary run
  state, ledgers, artifacts and child runs.
- **One system.** An Automation is its root run. Gateway APIs and indexes project runtime
  truth; they do not independently schedule or execute work.
- **Extensible triggers.** "Trigger" is the user concept. Runtime `TriggerSource` adapters
  register once and become discoverable to hosts and apps.
- **Visible and manageable.** Users can inspect results and steps, edit definitions,
  pause/resume, run manually, stop current work and archive.
- **Conversation representation.** Each occurrence contributes a trigger/task turn and
  answer. Independent mode starts fresh; Growing mode supplies bounded prior conversation.
  Long-term summaries remain target-owned in v1.
- **Isolated discussion.** Discuss creates a separate session seeded at a selected
  occurrence. It cannot write into automation context or resources.
- **Minimal scope.** Reuse commands, stores, effects, bundles and tool ceilings. External
  admission, automatic summaries and connector infrastructure are deferred.

Operator rulings (2026-09-26), which amend the design:

1. Runtime-root model with child occurrences: approved.
2. Independent mode is the default (matches today's `share_context=false` behaviour).
3. Schedule + manual first; external triggers are the next phase (planned item 0929).
4. **Discuss is NOT read-only and NOT tool-restricted.** A discussion is a new durable
   runtime session, forked/seeded from the automation's conversation through the chosen
   occurrence, replayable like any session, with the target's normal tools. Isolation means
   only that it never writes back into the automation's session/context. The
   `DISCUSSION_READERS_V1` allowlist in contract B is dropped. Open operator question: the
   discussion's workspace (own workspace with read access to the occurrence's, or shared
   read-write).
5. Quiet results (`notify:false` / empty answer stay quiet; failures and human waits always
   surface): accepted provisionally.
6. v1 apps are Observer and Assistant only. The console inventory (0930) and Code WUI/TUI
   move to the phase after v1.

## Scope

### In

- Every contract F route below, with the exact shapes and error envelope.
- `automation.*` command types on `POST /commands` and `POST /automations/{id}/commands`,
  both writing the same durable command store; application goes through the runtime.
- Runner changes: automation commands applied via `apply_automation_command()`; a failed
  application recorded as `automation.command_result{status:"rejected",error}` in the
  ledger (not only logged); `automation.resume` re-arms without firing.
- `session_kind` projection on `GET /runs`, run summaries and session listings; normal
  session lists include only `chat` and `discussion`.
- Discussion start: new `discussion` session, seeded once through the chosen occurrence
  via runtime `select_session_turns(..., through_occurrence=)`, the target's normal tools.
- Per-principal seen store (monotonic attention cursors).
- Legacy projection of `scheduled:<uuid>` wrapper runs, plus the `is_scheduled` fix on
  index-backed `GET /runs` rows.
- Catalog `automation_defaults` projection.
- Capability advertisement of the Automation API.
- Tests and the acceptance script listed below.

### Out

- Any gateway-side scheduler, execution registry or per-automation generated flow.
- Automatic legacy migration ("Recreate as automation" is a client action that calls
  `POST /automations`).
- External event admission, generic event sources, terminal chaining (0929).
- Console inventory and controls (0930).
- Automatic growing-context summaries (`context.growing.summary` returns
  `unsupported_feature`).
- Concurrent occurrences, delivery routing, constrained fetching.
- Multi-process runners on one store (v1 supports one active runner per store).

## Contract F (verbatim from the design)

```text
AutomationSummary={
 automation_id,title,status,trigger:TriggerBinding,context_mode,next_fire_at?,
 occurrence_count,last_occurrence?:{
  run_id,index,status,fired_at,finished_at?,excerpt,notify},
 attention:{pending_waits:int,unread:bool,cursor:string},
 legacy:bool,revision:int|null,updated_at,capabilities:string[],
 session_kind:"automation"
}
OccurrenceRow={
 run_id,index,fired_at,finished_at?,status,
 trigger:{source_id,summary},user_turn,answer,notify,
 artifacts:[{artifact_id,name,mime_type,url}],
 waits:[{run_id,wait_key,reason,prompt?,choices?}],
 ledger_url,workspace_url?
}
CommandReceipt={command_id,accepted,duplicate,seq}
```

| Route | Request → response |
|---|---|
| `POST /automations` | `{request_id,title,target,trigger,context?,policy?}` → `{automation_id,revision,summary}` |
| `GET /automations` | `status,changed_since,cursor,limit` → `Page<AutomationSummary>` |
| `GET /automations/{id}` | → `{definition,active_revision,summary}` |
| `PATCH /automations/{id}` | `{command_id,expected_revision?,changes:{title?,target?,trigger?,context?}}` → receipt |
| `POST /automations/{id}/commands` | `{command_id,type,payload?}` → receipt |
| `GET /automations/{id}/occurrences` | `cursor,limit` → `Page<OccurrenceRow>` |
| `POST /automations/{id}/discuss` | `{request_id,occurrence_index,prompt}` → `{session_id,run_id,session_kind:"discussion"}` |
| `POST /automations/{id}/seen` | `{attention_cursor}` → `{attention_cursor}` |
| `GET /trigger-sources` | → `{items:[TriggerSource+{available,unavailable_reason?}]}` |

`Page={items,next_cursor,change_cursor}`; opaque restart-stable cursors, explicit
invalidation after incompatible rebuild, archive tombstones retained.

Command types:
`automation.revise|automation.pause|automation.resume|automation.run_now|automation.stop_current|automation.archive`.
Reuse `/commands`; acceptance differs from ledger-recorded application outcome.

Command semantics (PLAN §2):

- **Pause:** suspend scheduled admissions; current occurrence finishes.
- **Run now:** allowed while paused; executes once and remains paused. Reject
  busy/exhausted/archived states. No queue.
- **Resume:** re-arm without firing or catching up paused time.
- **Revise:** activate at the next controller boundary; re-arm idle waits without firing.
- **Stop current:** cancel the occurrence tree only.
- **Archive:** prevent future admissions, finish current work, retain history.
- Normal downtime coalesces missed scheduled ticks into at most one occurrence.

Automation status: `active|paused|completed|archived|failed`.

Errors:

```text
{error:{code,message,field?,command_id?}}
404: automation_not_found|occurrence_not_found
409: revision_conflict|automation_busy|invalid_state|identity_conflict|cursor_expired
422: invalid_definition|unsupported_feature|unknown_trigger_source
```

Notification convention: successful `notify:false` or an empty normalized answer
suppresses unread/tray attention, never occurrence/change visibility. Failures and human
waits always demand attention. The gateway owns monotonic per-principal seen preferences.

Catalog defaults: `manifest.metadata.automation_defaults[flow_id]`; trigger pin optional,
prompt rendering only for compatible inputs.

Referenced runtime shapes (contract C, owned by AbstractRuntime):

```text
TriggerSource={
 id:string,version:int>=1,label:string,config_schema:JSONSchema,
 event_schema:JSONSchema,capabilities:{kind:"time"|"manual"|"event"}
}
TriggerBinding={
 binding_id:UUID,source_id:string,source_version:int,config:JSON
}
```

## Current code reality (verified 2026-09-26 at 00d6c66)

- `routes/gateway.py:2383` `ScheduleRunRequest`; `:2780` `_build_scheduled_wrapper_visualflow`
  builds a per-schedule wrapper VisualFlow; `:8145` `POST /runs/schedule` names it
  `scheduled:<uuid>` (`:8357`), uses the wrapper id as session prefix when
  `share_context=false` (`:8358`), registers it via `host.register_dynamic_visualflow`
  (`:8373`) and tags it `kind:"scheduled_run"` (`:8404`). This is the legacy path the
  projection must recognise, unchanged.
- `routes/gateway.py:8657` — `GET /runs` index-backed rows (`_summary_from_index_row`,
  `:8639`, used at `:8717`) hard-code `"is_scheduled": False` and `"schedule": None`; only
  the non-index fallback path calls `service.run_summary` (`:8756`). A legacy schedule listed
  through the index therefore reads as not scheduled. Fix alongside the projection.
- `routes/gateway.py:28465` `POST /commands` — a closed allowlist
  (`pause|resume|cancel|emit_event|update_schedule|compact_memory|inject_guidance|conclude`)
  with a hand-written 400 message; errors are `HTTPException(detail=str)`, not the contract F
  `{error:{code,...}}` envelope. The capabilities `commands.types` list (`:17021`) duplicates
  the allowlist and must be extended in the same change.
- `runner.py:1756` repeats the allowlist; `:1794` dispatches `update_schedule` to
  `_apply_update_schedule` (`:2698`), which only accepts runs inside a legacy scheduled tree.
- `runner.py:2084`-`:2088` — resuming a paused legacy schedule calls
  `_maybe_trigger_scheduled_wait_now`, i.e. **resume fires now**. Legacy keeps this; the
  automation path must not use it (resume re-arms without firing).
- `runner.py:1293` `_poll_commands` advances the command cursor even when `_apply_command`
  raises (`:1303`); the failure is only logged. For automation commands the outcome must be
  written to the ledger as `automation.command_result` before the cursor advances.
- `runner.py:2566` `_resume_subworkflow_parents` and `:1596`
  `_repair_terminal_subworkflow_waits` are the parent-resume/repair paths the controller's
  dispatch wait relies on; R reads them, G must not add a second resume path.
- `service.py:1225` `run_summary` computes `is_scheduled`/`schedule` from
  `_meta.schedule.kind == "scheduled_run"`; `service.py:272` `_config_for_principal` selects
  the per-principal data dir/store — the gateway, not the runtime, selects the plane.
- `hosts/bundle_host.py:2024` `_seed_session_history` seeds `context.messages` from prior
  completed root runs, called at `:2488` when `input_data.use_session_history` is true.
- `hosts/bundle_host.py:782` `register_dynamic_visualflow` / `:807` `upsert_dynamic_visualflow`
  persist wrappers under `dynamic_flows/` (created at `:1057`); `:884` already reads
  `manifest.metadata` (for `min_runtime`), the natural place for `automation_defaults`.
- `session_history_bloc.py:54` `list_session_root_turns` queries `root_only=True` (`:66`)
  and drops rows with a `parent_run_id` (`:80`): occurrences are child runs, so neither
  Growing-mode seeding nor the history bloc sees them today. Both must switch to runtime
  `select_session_turns(include_occurrences=True)` for automation sessions.
- No per-principal preference/seen store exists in the gateway source today; the attention
  cursor store is new.
- `tests/test_gateway_scheduled_runs.py` and `tests/test_gateway_schedule_thinking.py` pin
  the legacy path and must stay green.

## Seams

What this slice must READ in abstractruntime (live code, not the design) before writing:

- `apply_automation_command()` — signature, rejection reasons, how it records
  `automation.command_result`, and which lock it takes (the process-wide RLock pool,
  `core/runtime.py:492`).
- `Runtime.start(..., run_id=)` and `START_SUBWORKFLOW.payload.run_id` — create-if-absent and
  identity validation; `identity_conflict` maps from their failure.
- `select_session_turns(store, session_id, *, include_occurrences, before, automation_id,
  through_occurrence, limit)` — used for Growing seeding, history bloc and discussion seeds.
- `list_automations(...)`, `latest_occurrence(...)`, `list_run_index(..., automation_id,
  role, session_kind, changed_since)` — the only sources of `Page`, cursors and
  `session_kind`.
- Trigger descriptors from the `abstractruntime.trigger_sources` entry-point group
  (`TriggerSource`, `validate`), backing `GET /trigger-sources` and 422
  `unknown_trigger_source`/`invalid_definition`.
- The controller bundle registration helper for
  `abstractframework.automation-controller@1.0.0` (flow `controller`).

At the time of writing none of these exist in abstractruntime 0.5.1
(`Runtime.start` at `core/runtime.py:1292` has no `run_id`). Code in this slice calls them
directly and fails loudly if absent; no fallback shims.

What Observer and Assistant READ from this slice: the route shapes and error envelope
above, `session_kind` on runs and sessions, the attention cursor and `seen` semantics, and
the capability entry advertising the Automation API.

## Tests to deliver

- `tests/test_automations_api.py` — every F route: create/list/get/patch/commands/
  occurrences/discuss/seen/trigger-sources; exact shapes; each error code; duplicate
  `command_id` returns `duplicate:true` with the original `seq`; `expected_revision`
  mismatch → 409 `revision_conflict`; legacy schedule listed with `legacy:true`,
  `revision:null` and its existing controls; `is_scheduled` true on index-backed `GET /runs`.
- `tests/test_automations_attention.py` — quiet success does not mark unread but advances
  the change cursor; a failed occurrence and a pending human wait always mark unread;
  `seen` is monotonic per principal (older cursor ignored); reconnect with a stale change
  cursor yields `cursor_expired` or a correct page, never silent loss.
- `tests/test_automations_plane_isolation.py` — two principals: neither lists, reads,
  commands nor discusses the other's automations; normal session lists exclude
  `automation`/`occurrence` sessions and include `discussion`; a discussion never writes
  into the automation's session or context.

## Acceptance script

Deliver `scripts/accept_automations_v1.py`:

```sh
python abstractgateway/scripts/accept_automations_v1.py \
  --managed-gateway --data-dir /tmp/automation-acceptance \
  --interval 2m --occurrences 2
```

Behaviour:

1. Refuse to run unless `--data-dir` is empty.
2. Manage only its own gateway subprocess (start, restart, stop); touch no other process.
3. Register a deterministic fixture target flow that echoes the history it received,
   waits, and returns an answer; no provider required.
4. Create an automation; **pause** it; confirm no scheduled admission while paused.
5. **Run now while paused**: exactly one manual occurrence runs; the automation stays paused.
6. **Resume**: re-arms without firing and without catching up paused time.
7. **Edit the interval** (revise); the new interval takes effect at the next controller
   boundary without firing.
8. **Restart the gateway mid-occurrence**; after restart the scheduled run produces
   **exactly two distinct occurrence children** (deterministic IDs, no duplicate).
9. Prove context modes: in Independent mode each occurrence's echoed history is fresh; in
   Growing mode the second occurrence's echoed history contains the first occurrence's
   turn and answer.
10. Start a discussion at an occurrence and send **two discussion turns**; confirm the
    second sees the first, and the automation's session is unchanged.
11. **Replay** the automation and the discussion from the ledger with **zero provider and
    zero tool calls**.
12. Exit 0 only when every step passes; print one line per step with evidence.

## Definition of Done

- All contract F routes live with the exact shapes and error envelope; capabilities
  advertise the Automation API.
- `automation.*` commands accepted on both command doors; application outcomes visible in
  the ledger (applied and rejected).
- Legacy schedules projected with `legacy:true` and behave exactly as before (existing
  scheduled-run tests green); `is_scheduled` correct on index-backed rows.
- The three test files pass; the full gateway suite stays green.
- `scripts/accept_automations_v1.py` passes against the released runtime, including the
  restart step.
- `docs/api.md` documents the routes, shapes, errors and the notification convention.

## Risks

- **Double firing on resume.** The legacy resume-fires-now path (`runner.py:2084`) must not
  apply to automation roots; pinned by the acceptance script's resume step.
- **Silent command failure.** `_poll_commands` advances past a raising command; without a
  ledger `command_result` the client sees `accepted:true` and nothing happens.
- **Duplicate children after restart.** Owned by the runtime's deterministic create-or-load;
  the gateway must not start occurrences itself. Proven by acceptance step 8.
- **History blind to occurrences.** Root-only session selection (`session_history_bloc.py:54`)
  silently drops Growing context; must use the runtime selector.
- **Plane leakage.** Automations must be read only from the principal's own store
  (`service.py:272`); pinned by the isolation test.
- **Two error conventions** in one router (`detail` strings vs `{error:{...}}`); keep the
  new envelope confined to `/automations*` and `/trigger-sources`.

## Dependencies

- AbstractRuntime Automations v1 (contracts A–E) released first; this slice raises the
  runtime floor.
- Operator answer on the discussion workspace question before `POST /automations/{id}/discuss`
  is finalised.
- Consumers: abstractuic (fixtures from real responses), Observer, Assistant.

## Related

- abstractframework backlog 0928 (master item).
- abstractgateway 0930 (console inventory, after v1), 0929 (v2 external events).
- Legacy scheduled runs: `tests/test_gateway_scheduled_runs.py`.

## Contracts pass (2026-09-27)

Final contracts: untracked/design/automations-CONTRACTS.md (root repo; rev 2 with Astra turn-6 amendments 1–11). They supersede the contract text copied above; earlier text is kept as history. Concrete changes for this item:

- Error body is `{"detail":{"reason_code","message","field?","command_id?"}}` (repo convention), enforced by scoped app-level handlers for every non-2xx on `/api/gateway/automations*` and `/api/gateway/trigger-sources`, including 401 `unauthorized`, 403 `forbidden`, malformed JSON → 422 `invalid_request`. Replaces `{error:{code,…}}`.
- Full paths under `/api/gateway`. `changed_since` → 422 `unsupported_feature`; clients poll full paginated summaries; `Page={items,next_cursor}`; new `GET /api/gateway/automations/{id}/attention` (unseen, oldest first); summary `attention` gains `unseen_count`, `items` (oldest unseen first), `waits`.
- `abstractgateway/command_types.py` is the single command-type source (route `:28465`, description `:2446`, runner `:1756/:1772`, capabilities `:17021`) + AST single-source test; the runner calls runtime `record_automation_command_result` before the cursor advances; crash tests around cursor advance.
- Attention preferences: `<plane>/automations/attention/<sha256([tenant,user])>.json`, tuple stored and verified, serialized read-modify-replace; `/seen` keeps the max.
- `/runs` rows gain `session_kind, automation_id, role, occurrence_index`; `session_kind` filter added to the known-params list; `is_scheduled` on index rows = `role=="legacy_schedule"`.
- Discussion via runtime `start_discussion`; strip client `workspace_read_only`; `start_run` restamps discussion attribution + read-only workspace at entry (runtime `Runtime.start` is the authority) and seeds discussion sessions strictly (never the unseeded fallback at `bundle_host.py:2031`).
- Load the controller through `load_from_dir(extra_bundle_paths=[controller_bundle_path()])` as `private`/`framework`; hide `metadata.internal` bundles; resolve the persisted `abstractframework.automation-controller@1.0.0:controller`.
- VisualFlow save models accept `automation_defaults` (else `extra="forbid"` 422s it); publish writes `metadata.automation_defaults[root flow_id]`; `/bundles`, `/bundles/{id}`, catalog records expose it.
- `@default` targets resolved at create/revise with `resolve_default_agent_workflow`; the concrete target is stored.
- Acceptance script gains: one-shot exhaustion, kill between decision append and state save, retry (success on attempt 2 = no attention; 3 failures = one item), revise during backoff, EVENT-prompt wait counted, discussion `execute_python` and VisualFlow write refused, `changed_since` → 422.
