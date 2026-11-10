### Changed

- **The gateway half of the Stop fence** (STOP-LANE-DESIGN 2026-11-09 §2.2; AbstractRuntime 66d3220..0da3f74). Every
  runtime state writer the gateway calls carries a fence: the kill switch writes with `KILL_SWITCH`, Resume with
  `RESUME`, everything else with the `LifecycleFence` of its admission (a door's generation, or the generation read
  under the lifecycle lock for a write decided in that step).
  - The sidecar is written only from inside `fenced()` / `fenced_here()`: `write_record` takes only the record such a
    block yielded, while it is open; anything else is a `TypeError`.
  - `read_record` fails closed: an unreadable `phases.yaml` is an unknown generation (never the sidecar's old number),
    and nothing is admitted on it. The life_state read is still served, with a `#FALLBACK` warning and nothing recorded.
  - The hosted chat opens its home and its `ChatSession` with the visit's fence (the session's every model call,
    tool and effect checks it; `cancel_check` is gone). The close's restore and an aborted open's restore are fenced on
    the visit's admission, like the durable visit's terminal restore (its generation is stamped in `_visit.generation`).
  - The registry opens each entity runtime with a fence admitted at the home's generation and keeps it; a door that
    admits new work after a Stop readmits it at its own generation (`runtime_for_admission`, AbstractRuntime
    `EntityRuntime.readmit`), Resume readmits a cached one, and a visit run admitted before a Stop is refused at its
    door. A run the fence cancelled is answered with "Stopped. Resume to visit.", never its error line.
  - The registry's shared home (`EntityHome`) gives memory a commit guard and the book a guard that check the fences
    of the act in progress (`entity_phase.acting_under`): the operator sleep's and the idle sleep's night run under the
    pass's fence (every commit refused after a Stop, memory's `cancelled=` checks the fence and re-reads
    `phases.yaml`, fail closed), and a Stop refusing a commit ends the night as stopped by the operator, never failed.
  - Reconcile has no spawn claim with a clock: a start in progress is the runtime's reservation (loop_status phase
    `starting`) or this process's own decision-to-reservation window; the runtime's "already starting" / "already in
    progress" refusals read as `spawning`, its `StoppedByOperator` as `stopped`. The idle sleep reads the same.
  - The visit waits check the runtime fence (a compare plus one stat), never a sidecar and `phases.yaml` re-parse
    every 250 ms.
  - The Stop's receipt carries a `signal` row (`failed lifecycle_signal_unwritten`) when the runtime moved the
    generation but could not write its signal file (`LifecycleSignalUnwritten`): the Stop stands, other processes
    see it at their next read. The start doors answer a start the Stop overtook (`StoppedByOperator`) with
    "Stopped. Resume him first." and a start another one beat ("already starting", `LoopAlreadyRunning`) as
    `already_running`, never `start_failed`.
- Tests stub only the loop's process creation (Codex review 3 #12): `subprocess.Popen` answers the runtime spawn's
  own argv with a fake child (its pid beyond any pid_max), so the spawn lock, the start refusal, the lifecycle fence
  and the reservation run in every test; `loop_spawns` records the loops a test's code tried to create.
