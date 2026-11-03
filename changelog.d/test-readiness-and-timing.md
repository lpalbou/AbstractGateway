### Changed

- **Tests: a real server is used only once it answers HTTP; wall-clock bounds are opt-in.** Every test
  that starts a real `abstractgateway serve` waits through one shared helper (`tests/serve_ready.py`,
  `wait_until_answers`) until `GET /api/health` answers with the boot over (a status other than
  `starting`), instead of a boot log line or audit record that
  is written before the port is bound (`test_a_real_serve_audits_the_flag_at_boot[True]` was refused a
  connection on a 3.10 main run). Elapsed-time assertions now go through `tests/timing.py`: reported by
  default and enforced only with `ABSTRACTGATEWAY_TIMING_STRICT=1`; where the intent is "did not block",
  an ordering or event check replaces the stopwatch (a host rebuild that holds until `/api/health` has
  answered, a read that returns while a writer still holds the memory home, configuration reads that
  return beside a frozen model-host child, a Stop that leaves the stream unfinished, a watchdog that
  ends the stream before the engine's first audio). Every test-local wait helper's bound is `patient()`
  (x5 unless strict).

### Fixed

- **Push: an event appended by another process while a pass ran is no longer slept past** (split mode). The
  dispatcher took the event stream's change token when its wait started, after the pass had read the stream,
  so an append written by another process during the pass (no listener fires across processes) was already
  in the token and the wait slept until the next registration or setting change. The token is now taken
  before the pass reads, like the registration and setting tokens (backlog 1009). Pinned by
  `test_an_append_from_another_process_during_a_pass_is_not_missed`.
- **MCP connection test: a server that never answers is always reported as "did not answer initialize
  within N seconds".** The MCP client's own request timeout equalled the test's deadline, so it could fire
  first and the Settings sentence became "request timed out after 1.0s"; it is now a backstop set 2 s past
  the deadline (`CLIENT_TIMEOUT_GRACE_S`).
