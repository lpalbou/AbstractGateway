### Fixed

- **The Stop receipt lists only the runs it found live.** The summon seat outlives its run, so a Stop
  after a finished summon listed that run (`outcome: already_terminal`), and a seat naming a pruned run
  listed it as `missing`; a visit's finished turns were listed the same way. `items` now carries a `run`
  row only for a run the Stop cancelled or could not cancel (`failed`, retried by the next Stop), on all
  three paths (the summon seat, the open visit's tree, a repeated Stop's retries). `cancelled.runs` is
  unchanged. The per-run helper `cancel_run_tree_outcomes` still reports every outcome.
