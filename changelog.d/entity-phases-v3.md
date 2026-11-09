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
