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
