### Fixed

- **Opening a visit never fails because the entity is busy** (operator 2026-11-09; root ADR-0027). Both visit doors
  (`POST /entities/{name}/visit/open` and `/chat/open`) waited at most 55 s for the entity's own-time loop to reach a
  tick boundary, then rolled the state back to awake and answered 409 `loop_busy` ("retry shortly"), so the loop never
  learned a visitor had come. They now wait with no time limit; with AbstractRuntime's fix the loop starts no new tool
  once the visiting posture is written and yields right after the step in progress. The stale-yield reaper leaves a
  visiting posture alone while the loop is still in its day or while a visit is opening on that home, and a door that
  is still waiting writes its posture back if something replaced it (an operator wake click during "Opening…"). A home
  held by another writer (a sleep's consolidation pass) makes the open, a turn or a close wait for it (logged once,
  naming the holder) instead of answering 409 `home_lease_held`; the reaper's sweep and the operator's own pause/sleep
  teardown never wait (the reaper retries next sweep, the teardown stays labeled `teardown_failed`). `loop_busy` and `home_lease_held` are gone from the documented refusals. Needs the AbstractRuntime
  release with `await_loop_quiescent(timeout_seconds=None)` and `ChatSession.yield_check`.
