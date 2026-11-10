### Fixed

- **An operator sleep on a sleeping entity adds no sleep rows** (`entity_phase.py`, `entities.py`). The door
  takes a sleep asked for while he already sleeps as the same sleep (with its maintenance pass when `dream`),
  yet its new reason and the pass's dreaming mode each wrote a `phase_changed` "sleep → sleep" row, and the
  pass ended with a `sleep` moment ("Fell asleep") for a sleep that had begun long before. A sleep that is
  already the current phase now writes no `phase_changed` row and no `sleep` moment: `GET .../life_state`
  still versions the new details (`rev` moves), and the pass tells its own story (the night's records, then
  the wake and the sleep → idle row when its maintenance is done). The identity card's moments no longer
  count a state-history line that repeats the standing state (a new reason, a dreaming mode, a visit's
  posture on a sleep). A sleep from idle, a gate sleep included, still falls asleep with both rows.
