### Added

- **An idle entity sleeps on its Mind's thresholds** (contract-phases v3 D2, operator 2026-11-09). The cognition
  profile gains `sleep: {after_hours: 24, after_events: 500}` (served and set through `…/cognition-profile`;
  hours 1 to 720, events 1 or more; AbstractRuntime 23a79d6). The gateway's sweep puts an IDLE entity to sleep once
  either is reached since its last completed sleep (AbstractMemory `records_since_maintenance`, seeded with the home's
  own last completed maintenance so existing homes do not all sleep at once). The sleep is its own (`kind: self`,
  the reason names the threshold), runs its maintenance pass and ends idle; host marker `idle_sleep` records the
  threshold, the counts and which clock fired (`boundary_source`). One home sleeps per sweep, the one waiting
  longest first. An idle home under its thresholds gets the authoring drainer instead.
