### Changed

- **There is no work switch; jobs drive work** (contract-phases v3 D1/D4, operator 2026-11-09). `life_state.toggles` is
  `{personal}`; `can.toggle_work` and the `work_off` refusals are gone, and `PUT …/life_state/toggles` with `work`
  answers 422 `unknown_field`, "There is no work switch: work starts when a job is queued." `life_state.work` is
  always `{jobs_queued, current}` (`current`: `{kind, run_id, since}`, kinds `summon`, `automation`, `work_order`,
  `task`). A work order or a task queued on an idle entity starts the loop for work (`return_to: work`) without the
  personal grant; a job during own time interrupts it, and when all jobs are done he returns to own time only while
  it is still given, else idle. A loop that ends on its own with jobs left is not respawned by that end.

- **Stop takes own time back; Resume is idle** (contract-phases v3 D3, operator 2026-11-09). `POST …/lifecycle
  {action: "stop"}` also revokes the personal grant (marker `personal_grant_revoked`, `revoked_by: kill_switch`) and
  forgets the phase to return to. When a grant was on, its answer carries `own_time_revoked: true` and the row
  `{item: "own_time", code: "revoked", outcome: "done", sentence: "Own time was revoked."}`.
  `resume` writes awake and the entity is `idle`: nothing from before the Stop is restored, and own time starts only
  when the operator turns personal on again.
- **The runtime owns the job queue and the way back** (AbstractRuntime 1b1db48, ed32f0e, 901d9c5). `work.jobs_queued`
  is the runtime's `read_job_queue` (the work order, then taken and pending tasks). `resume_to` is read and written
  only through the runtime's `read_resume_to` / `write_resume_to` bucket, which the loop and the kill switch write too.
  A loop started for queued jobs is told no `return_to` (its gate works the queue). `jobs_done` is a requested end
  (a clean idle, never a crash).
- **Canonical phase spec v25** re-vendored byte for byte (abstractentity 814c574): the newborn is idle, the
  `work->personal#operator` edge is gone, and the guards into work are `job_queued`.
- **A job never erases the own time it interrupted** (Codex review 2 #6). The queue's work day no longer overwrites
  `resume_to = personal`, and a remembered `work` return is never turned into own time just because the grant
  stands.
- **Nothing gives own time back across a Stop** (Codex review 2 #7). The deprecated `PUT …/personal-grant` is
  refused while stopped (409 `stopped_use_resume`), checked and armed in one step under the lifecycle lock; Resume
  forgets the way back and revokes any grant armed meanwhile, so it always lands in idle.
- **A visit waits; it is never refused for another visit** (Codex review 2 #9; root ADR-0027). A visit door that
  finds another person's visit or a drawer session on the entity waits, without a deadline, for it to end (only
  Stop ends the wait). The same visitor opening from another conversation is a handoff: their visit there closes
  and this conversation's visit opens. Participants stay isolated: nobody joins another person's visit.
- **Stop never takes an unknown process for a dead one** (Codex review 2 #4). The loop's liveness is alive, dead or
  unknown (EPERM, an unreadable start time): unknown is not signalled and not counted as stopped, the Stop's loop
  row says `loop_unverified`, and pressing Stop again retries.
- **One lock and one generation with the runtime** (Codex review 2 #1; AbstractRuntime f881cac). The gateway's
  lifecycle lock now holds the runtime's home state lock inside it, so every gateway check-then-write is serialized
  with the loop's own state and `phases.yaml` writes; the lifecycle generation is the runtime's
  (`read_lifecycle_generation`, bumped by every Stop through `bump_lifecycle_generation` and by the runtime's hard
  stop). The operator's state write (`POST …/state`) is fenced on it: a Stop landing meanwhile is never written over.
- **Stop reaches every effect boundary** (Codex review 2 #2; AbstractRuntime ab060bf). A hosted chat turn checks
  the lifecycle generation at each boundary (`ChatSession.cancel_check`): after a Stop it runs no further tool, model
  call, formation or authoring, and its record says cancelled, by the operator (the turn answers 409
  `stopped_use_resume`). The operator's maintenance pass stops at its next stage on a Stop or a visitor
  (`should_continue`), and the authoring drainer stops before any further call or write (`should_stop`).
- **A mirrored mention is admitted in one step** (Codex review 2 #3). The mentioned entity's generation check and
  its join to the other visit happen under its lifecycle lock, and the generation is checked again right before the
  mirrored turn: a Stop of the mentioned entity carries none of his words into another visit. (A Stop landing
  while the visited entity is already answering cannot cancel that other entity's turn.)
