### Added

- **A lean entity tile read: `GET /entities/{name}/tile`** (`entity_tile.py`, backlog 1012). One small card about an entity
  read the whole identity card (up to 90 KB and 0.5-1 s warm for a long life such as ephemeral). The tile serves exactly what a
  card shows a person, about 2 KB: name, look, age, its model; what it is doing now and since when (the same phase
  `/cognition` folds, busy, and a warning when its own time stopped after repeated failures); its newest interests, its newest
  open problem or question, its latest lesson, its last meaningful moment (a closed set of kinds: visits, sleeps, its own
  time, phase and model changes, never the operator reading its diary or the birth recording of its identity) and counts.
  The roster (`GET /entities`) and the discovery contract (`common.entities.tile`) advertise it; clients fall back to the
  card on older gateways. Measured on a copy of ephemeral: 7-9 ms warm.

### Changed

- **The identity card is computed once per state of a life** (`EntityHome.engine_card`). The engine compositor's result and
  the newest model stamp are cached per (home directory, journal seq, parameters) — the communities rule, tenant-safe,
  bounded to 64 entries — and the merged host moments per (home, marker and state-history file size and time). A repeated
  card read of an unchanged life costs 6-12 ms instead of 0.2-0.6 s (castor, ephemeral); a grown life recomputes once.
  `/cognition` and `/tile` share one live fold of the phase (`_live_now`). Host-marker moments now carry `details.change`
  for a work-order change (`set` or `cleared`).
