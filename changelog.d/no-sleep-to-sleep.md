### Fixed

- **A visit on a sleeping entity no longer leaves a "sleep → sleep" row.** castor and nerin carried
  `phase_changed` markers with `from == to == "sleep"`: each visit's close wrote the sleep back changed
  (its reason, its start, and its mode, which the visit's open did not record), so the snapshot moved
  without a phase change. The open now records the sleep's mode with the rest of the prior state, and
  the close writes back exactly the sleep it interrupted, so a visit that nobody watched adds its own
  rows (`summon`, `session_closed`) and nothing else. A restore onto a state that already is that sleep
  writes nothing.
