### Changed

- **Tests: a real server is used only once it answers HTTP; wall-clock bounds are opt-in.** Every test
  that starts a real `abstractgateway serve` waits through one shared helper (`tests/serve_ready.py`,
  `wait_until_answers`) until `GET /api/health` answers, instead of a boot log line or audit record that
  is written before the port is bound (`test_a_real_serve_audits_the_flag_at_boot[True]` was refused a
  connection on a 3.10 main run). Elapsed-time assertions now go through `tests/timing.py`: reported by
  default and enforced only with `ABSTRACTGATEWAY_TIMING_STRICT=1`; where the intent is "did not block",
  an ordering or event check replaces the stopwatch (a host rebuild that holds until `/api/health` has
  answered, a read that returns while a writer still holds the memory home, configuration reads that
  return beside a frozen model-host child, a Stop that leaves the stream unfinished, a watchdog that
  ends the stream before the engine's first audio). Every test-local wait helper's bound is `patient()`
  (x5 unless strict). No product code changes.
