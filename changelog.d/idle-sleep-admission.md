### Fixed

- **A Stop during a sleep's final stage no longer records a completed night.** The maintenance pass (the operator's
  sleep and the idle sleep) now also hands AbstractMemory its Stop check as `sleep_pass(cancelled=)` (AbstractMemory
  f310865), consulted right before the night's completion marker, so a Stop while the dream ran no longer resets
  the idle sleep's boundary. The check fails closed: a lifecycle generation that cannot be read ends the night like
  a Stop (it used to let the pass go on). Needs the AbstractMemory release carrying f310865.
