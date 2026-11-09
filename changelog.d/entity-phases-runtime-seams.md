### Changed

- **The own-time process is told where it returns** (AbstractRuntime `spawn_loop_process(return_to=)`, 2026-11-09).
  The operator's own start (personal on, `loop/start`, Retry) says `personal`; a restore after an interruption names
  the phase it took over (`personal` or `work`).

### Fixed

- **Own time that ends on purpose is not a crash.** A loop that stopped because work was turned off, a step was
  removed, or it had no own time to return to (`work_off`, `edge_removed`, `no_return`) leaves the entity awake and
  `idle` with no `loop_crashed` block; the sleep the loop's gate wrote is repaired at once.
- **A refused hard stop says why.** When AbstractRuntime refuses to signal a process (it stops only a verified loop of
  this home) or the process survives, the Stop receipt's loop row carries the runtime's reason.
- **A failed model call no longer ends a visit, and a mention says so plainly** (AbstractRuntime 1306804). A visit
  turn whose model call failed is answered by the runtime with the error and the visit stays open; the visit door's
  turn answer now carries `turn_error`. A mention whose own answer failed is still 503 `entity_cannot_answer` with the
  plain sentence (the error is never shown to the caller). A mirrored turn the visited entity could not answer shows
  "This turn could not be answered: …" as its `reply`, with `turn_error`.
