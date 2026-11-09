### Fixed

- **`life_state.state` follows the phase.** It served the stored state word, so it read `asleep` beside
  `phase: visit` during every visit (the visit door stores its posture as asleep + `state_mode: visiting`)
  and beside `phase: idle` after a gate sleep (root 1178). It is now `paused` while stopped, `asleep` in a
  sleep and `awake` otherwise. `state_mode` and `state_reason` still carry the stored record, and
  `GET /entities/{name}/state` still serves it whole. No client read the old value (the Entity app's
  `phase_bar_source.ts` and `stream_source.ts` do not read `life_state.state`).
