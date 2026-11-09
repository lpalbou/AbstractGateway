### Fixed

- **Only a maintenance pass that ran to its end is the home's last maintenance.** A night that memory
  ended early (it yielded to a visit at a stage boundary, or stopped on its hard check, such as an
  unreadable lifecycle generation) was recorded as `last_maintenance: done`, and that receipt reset the
  idle sleep's clock (24 h / 500 memories since the last real sleep). Such a night now records its own
  status in the phase sidecar's `last_pass` (`yielded` or `cancelled`, with memory's `cancelled_after`
  stage), and `last_maintenance` keeps the last pass that completed. Both callers (the operator's sleep
  through `POST /entities/{name}/state` and the idle sweep) pass memory's `cancelled_after`.
