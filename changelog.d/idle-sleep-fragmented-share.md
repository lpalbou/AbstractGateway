### Added

- **An idle entity also sleeps when too much of him is fragmented** (operator 2026-11-10; AbstractRuntime
  `sleep.after_fragmented_share`, default 0.15; AbstractMemory `records_since_maintenance(...).fragmented`). Beside
  `after_hours` and `after_events`, the idle sweep puts him to sleep once more than that share of his memories are
  not yet whole (placed provisionally in a topic, without a vector, or with a title or summary still being
  written), as long as something formed since his last completed sleep; the sleep's reason says it ("18 % of his
  memories are not yet in a topic") and the receipt carries memory's `fragmented` counts. One home per sweep, as
  before.
