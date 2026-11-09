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
