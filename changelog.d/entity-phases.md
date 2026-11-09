### Added

- **The kill switch: `POST /api/gateway/entities/{name}/lifecycle`** `{action: "stop" | "resume" | "retry"}`
  (operator ruling 2026-11-08; contract `CONTRACT-phases.md` v2 §3; admin-only).
  - `stop` is instant and unconditional, in a fixed order:
    1. The home's lifecycle `generation` is bumped first. This is the fence.
    2. `paused` is written.
    3. Cancellation is signalled at once: the loop's durable stop command, the summon seat's run tree, an in-flight
       visit turn's run tree, and the queued admissions (failed with "Stopped. Resume him first.").
    4. Each teardown then runs on its own: the loop gets SIGTERM, then SIGKILL after 3 s; the durable visit closes with
       `closed_by=pause`; the hosted chat closes without reflection.
  - The response lists one row per item (`run`, `visit`, `chat`, `loop`, `maintenance`, `queue`) with a stable
    `outcome` and, on failure, a `code` and a sentence. It also carries `complete`.
  - A repeated Stop retries whatever is still outstanding.
  - Every writer that began before a Stop gives up instead of undoing it: the unbounded visit wait (both lanes), the
    home-lease wait, an operator sleep's maintenance pass (`finally`), and a mention's answer (neither recorded in the
    session nor mirrored).
  - `resume` clears stopped, writes awake (never a sleep) and restarts own time when `personal` is on.
  - `retry` re-runs the gateway's own-time reconcile.
- `entity_seat.cancel_run_tree_outcomes`: one outcome per run (`cancelled`, `already_terminal`, `missing`, `failed`
  with `run_cancel_failed` or `run_children_unknown`) instead of a log line. `cancel_run_tree` keeps its shape.

- **`GET /entities/{name}/life_state` serves the phase bar** (contract v2 §1).
  - `phase` is exactly one of `visit | work | personal | sleep | stopped | idle`, and comes from ONE derivation
    (`entity_phase.derive`) that `/cognition`, the tile and `POST /state`'s settled answer now share.
  - New fields: `toggles {work, personal}`, `stopped`, `since` (the phase's entry), `sentence` (one plain line,
    no clock time), `next`, `blocked {code, sentence, action}`, `can {visit, sleep_now, toggle_work,
    toggle_personal}` (each `{allowed, sentence}`), and the `visit`, `work` and `sleep` sub-objects.
  - `rev` versions the whole snapshot: it is monotonic per home and survives restarts (sidecar
    `entities/.life_phase/<slug>.json`). Each new rev is announced once as a `phase_changed` host marker on the
    replay stream. If the announcement is missed, the next read sends it.
  - An unknown entity answers 404 `entity_not_found` with `life_state: null`.
- **`PUT /entities/{name}/life_state/toggles`** `{work?, personal?}` (admin-only; contract v2 §2).
  - The toggles are permissions. Personal on starts own time when nothing else holds the phase.
  - Personal off ends own time at the loop's next boundary and never puts him to sleep. The old gate's
    leftover sleep (`grant-gate`/`day-gate`) is written back to awake (root 1178).
  - Work off refuses new work, 409 `work_off`, at the summon door (direct and queued), the task inbox and the
    work order (clearing an order is never refused). The current run finishes.
  - A person principal's own summon (`caller_kind: human`) is a visit and is not gated. The declaration alone
    never qualifies.
  - The body is validated by the handler: 422 `invalid_body` or `invalid_toggles`. While stopped: 409
    `stopped_use_resume`.

- **The gateway owns own time** (contract v2 §4). Reconcile starts the own-time loop when `personal` is on and nothing
  else holds the phase.
  - Triggers: a durable visit's close (normal or idle reaper), a hosted chat's close, a summon run's end (runner hook),
    the work order being cleared, a graceful loop stop completing, a toggle, resume, retry, and an operator sleep's
    maintenance being done.
  - Never under a visit (open or opening), a running sleep, live work, the kill switch, or an archived entity.
  - Never twice: a spawn claim in the sidecar. A Stop that lands during the spawn kills the new loop.
  - No automatic respawn after a crash: the read shows `blocked {loop_crashed, action: retry}`.
  - Gateway shutdown never starts own time.
- **A visit's close returns to the previous phase** (§4.4). An interrupted sleep continues as the same sleep, keeping
  who chose it (`written_by`), its wake time and its original `since`. Both lanes restore `written_by`.
- **An operator sleep wakes when its maintenance is done** (`POST /state {state: sleep, dream: true}`; §4.3). The
  sleep is announced before the pass. When the pass returns he wakes and the gateway reconciles, unless a Stop moved
  the generation (nothing written) or a visit took the posture (the wake then runs at the visit's close). The response
  carries `maintenance: woke | after_visit | stopped | not_ours`.
- **The replay stream observes changes made inside the loop** (§5). When the home's `state`, `loop_status` or
  `phases.yaml` moves, and on every heartbeat, an open `/replay/stream` re-derives the snapshot. A new rev reaches the
  client as its `phase_changed` envelope, and a reconnect with `Last-Event-ID` redelivers it.

### Changed

- **Stopped is the one visit refusal** (operator 2026-11-09: a visit never fails). Both visit doors and the mention door
  answer 409 `{"detail": "Stopped. Resume to visit.", "code": "stopped_use_resume"}`.
  - `entity_asleep` is gone: an open visit's turns are never refused because of a sleep.
  - `entity_paused` is replaced by `stopped_use_resume`.
  - Hosted-chat refusals that carry a code are served as `{detail, code}` with the `X-Gateway-Error-Code` header.
- The durable visit status (`GET /entities/{name}/visit`) also carries `participants` and `created_at`.
- **Idle is served as `idle`, stopped as `stopped`.** `/life_state`, `/cognition` and the tile no longer fold
  "awake with nothing running" into `sleep` (the c203 IDLE-IS-SLEEP rule is retired by the 2026-11-08 ruling). Under
  the kill switch, `phase` is `stopped` instead of `null`. The tile's last moment skips a `phase_changed` echo of an
  act that is its own moment.
- The summon door's paused refusal also carries `reason_code: stopped_use_resume` and a `message`.
- **Operator sleep only changes the phase** (§4.3). It no longer disarms the personal grant or clears the standing
  work order: v17's OPERATOR-SLEEP-IS-ABSOLUTE composite is retired by the 2026-11-08 ruling.
