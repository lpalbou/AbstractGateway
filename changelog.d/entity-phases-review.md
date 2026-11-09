### Fixed

- **The kill switch's hard stop is safe and honest** (Codex review 2026-11-09 #3).
  - It runs once per home at a time, across threads and processes.
  - It reads the raw status, not the folded `running`, so a live loop behind a stale heartbeat is still stopped.
  - It never signals this gateway's own pid.
  - Before any signal it verifies the target's incarnation (pid plus the OS start time the loop stamped).
  - It reports a kill only when the death is confirmed: the runtime's `killed` when it says so, else the gateway's own
    check of that exact incarnation (zombies count as gone).
  - A loop spawned across a Stop is stopped the same way.
