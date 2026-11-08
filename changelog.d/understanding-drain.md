### Added

- **An entity rebuilds its memories after a visit turn: the gateway's share of the understanding drain, part one**
  (`entity_drain.py`, `entity_visits.py`, `entities.py`; runtime 0869 / gateway 1013). Once a turn has parked and the
  reply is in hand, the visit host runs that turn's share of the home's memory rebuild queue on a small worker of its
  own (one window per home at a time, never on the request thread, never on the shared pools): AbstractRuntime's
  `identity.understanding.drain(trigger="turn", window="visit", hard_cap=<the profile's rebuild.cap_per_turn>,
  budget_seconds=cap × 60, parent={visit_run_id, turn_id})`, with `should_continue` false as soon as a newer message
  reaches the visit, the home is paused or the gateway shuts down. The reply is never delayed. The turn response and
  probe gain `rebuilt: [{task_id, kind, record_id, outcome, version, model}]`, what the previous turn's window
  rebuilt (the report also rests in the run's `_visit.last_drain`); when the drain could not run, `notices` carries
  `#FALLBACK memory rebuild did not run: <error>`. `cap_per_turn` 0 starts no window. Each capability of the entity's
  thinking (`summaries`, `understanding`, `extraction`) gets its own client from its cognition profile
  (`EntityRegistry.capability_llm`, else the mind), with the entity patience window and never the mind's MTP
  speculation. Every entity home the gateway opens now has its memory task queue on (`MemorySystem(task_queue=True)`),
  without which no record would ever enqueue a rebuild. The shutdown sequence gains `entity memory drains stop`.
  Requires the AbstractRuntime release that ships `identity.understanding` (until then every window records that
  error and the probe says so) and the AbstractMemory release that ships the task queue.
