### Fixed

- **The kill switch's hard stop is safe and honest** (Codex review 2026-11-09 #3).
  - It runs once per home at a time, across threads and processes.
  - It reads the raw status, not the folded `running`, so a live loop behind a stale heartbeat is still stopped.
  - It never signals this gateway's own pid.
  - Before any signal it verifies the target's incarnation (pid plus the OS start time the loop stamped).
  - It reports a kill only when the death is confirmed: the runtime's `killed` when it says so, else the gateway's own
    check of that exact incarnation (zombies count as gone).
  - A loop spawned across a Stop is stopped the same way.
- **Stop fences first and never waits on cognition** (review #4). The interrupted phase is noted under the lock: no
  publish, no restore before the fence. An in-flight hosted chat turn is told to yield at its next model or tool
  boundary (`ChatSession.yield_check`) and is never waited on. The chat leaves the live index at once and closes, with
  no reflection, when the turn lets go (row `chat: cancel_requested`).
- **A repeated Stop retries what the last one could not finish** (review #5). Each failed run row records its store
  (`gateway` or `home`), and the next Stop cancels it there before the outstanding list is replaced.
- **One admission arbitration per home** (review #1 and #9). Every state write by a visit door, a visit restore and the
  maintenance pass is checked and written in one step under the lifecycle lock:
  - against the generation the writer saw at its entry (both doors' posture writes and their wait's hold, the
    restores after a close or an aborted open, the pass's own writes);
  - a Stop landing between a check and a write is never overwritten;
  - the unbounded visit wait also ends on a Stop followed by a Resume (generation, not only `paused`).
  - A visit taking the phase moves `admission_seq`, so an own-time spawn in progress is stopped and the visit wins.
- **A sleep never tears down a visit admitted during its maintenance pass** (review #7). The old post-pass teardown is
  gone; the sleep is refused up front while a visit is live (`in_visit`).
- **A failed maintenance pass completes with a receipt** (review #8). He wakes, and the sidecar records
  `last_maintenance {status: failed, error}`. A pass whose home was taken before it began is `superseded`. A stale
  completion never touches a newer pass's record.
- Test fake loops carry the loop module in their argv: the runtime's hard stop now checks the command line before it
  signals.
- **Mentions are fenced by the kill switch at every persistence step** (review #2). The answer is recorded under the
  lifecycle lock, against the generation the door saw. A Stop that landed since means nothing is recorded and
  nothing is mirrored. Each mirror is admitted only while the mentioned entity is not stopped.
- **`mention_id` is durable** (review #12). One record per conversation and `mention_id` in the plane's data folder,
  written atomically with the answer before the session turn is saved, with the mirrors' completion recorded
  separately. A resend reads it directly (no bounded scan) and re-saves a session turn a crash left unsaved.
- `stopped_use_resume` passes through the mention door's refusal mapping intact (review #6, mention half).
- **No visit refusal outside the kill switch at the doors** (review #6).
  - A live summon makes both doors wait, without a deadline, for its turn to end; it is no longer a 409
    `summon_seat_live`.
  - Another visit still opening makes the door wait until that open registers or goes, then pass every gate again;
    no more "retry shortly".
  - The same visitor opening their own live visit again (a reload, a second window) joins it: 200 `joined: true`,
    still exactly one run.
  - Every wait ends only on a Stop (generation).
  - A different person opening while someone else's visit is live still answers 409 `visit_already_open`.
- **A request that interrupted own time keeps the way back** (review #10). Only an own-time work day ending clears
  `resume_to`; a summon or automation ending is the end of an interruption, so a read racing the run's end hook now
  restores own time instead of erasing it.
- **The self-repair sweeper no longer respawns crashed or culled loops** (review #11). It records the death once,
  tells the operator and marks the biography; the loop stays down (`blocked {loop_crashed, action: retry}`) until the
  operator's Retry. The loop-less need-check (work orders) is unchanged pending the work redesign.
- **A reused mention in a visit lane never re-quotes the lane** (review #13). A per-entity, per-visit cursor kept with
  the conversation's mention record skips the lane turns the mentioned entity's visit already holds, mirrored turn
  included.
- **The kill switch keeps the ledger clean** (operator 2026-11-09: "the ledger must be kept clean"). Every run, visit,
  turn and pass it ends leaves one clean terminal record: status `cancelled`, reason `kill_switch: stopped by the
  operator`, by the operator. Never a failure, never a half-written entry.
  - Runs are cancelled with `cancelled_by="operator"` (the runtime closes an in-flight effect's ledger record as
    `cancelled`).
  - An open durable visit, parked or mid-turn, is cancelled, never "completed" by a hard-freeze close; its
    `session_closed` marker says `status: cancelled` and the reason.
  - A hosted chat closes with `status: cancelled, closed_by: kill_switch, by: operator`.
  - A maintenance pass ends with the receipt `last_maintenance {status: cancelled, reason: kill_switch, by: operator}`.

### Changed

- **A newborn entity starts idle** (operator ruling 2026-11-09). It is awake and reachable, with nothing of its own
  running, and own time starts when the operator gives it. Before, newborns were born asleep (the c1503 birth sleep).
