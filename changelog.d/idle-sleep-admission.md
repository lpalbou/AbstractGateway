### Fixed

- **An idle entity's own sleep can no longer be left standing, or written over a Stop, a visit or a job**
  (Codex review 3 #1, #10, #11). The sweep's sleep now keeps the lifecycle generation its idle check read (a Stop
  landing while the thresholds were read became the sleep's own generation: the entity ended asleep, then awake,
  after a Stop), takes the home's writer before its posture changes (a home someone else was writing was put to
  sleep with its maintenance pass skipped, and that sleep never ended), and checks again under the lifecycle lock
  that the entity is still idle, with no job queued or starting and the very state the check saw. Refused, nothing
  is written and the next sweep looks again. The runtime's own Stop (`hard_stop_loop`, which writes no state) is
  refused through the generation. One home sleeps per sweep, the one waiting longest first (the later of its last
  completed sleep and its last turn); a home refused at its admission keeps its place and the next due home sleeps
  instead. `EntityRegistry.set_state` gains `admission=(generation, admit)` for this; the operator's sleep is
  unchanged (it still sleeps when the home is busy, its pass skipped).
- **A Stop during a sleep's final stage no longer records a completed night.** The maintenance pass (the operator's
  sleep and the idle sleep) now also hands AbstractMemory its Stop check as `sleep_pass(cancelled=)` (AbstractMemory
  f310865), consulted right before the night's completion marker, so a Stop while the dream ran no longer resets
  the idle sleep's boundary. The check fails closed: a lifecycle generation that cannot be read ends the night like
  a Stop (it used to let the pass go on). Needs the AbstractMemory release carrying f310865.
