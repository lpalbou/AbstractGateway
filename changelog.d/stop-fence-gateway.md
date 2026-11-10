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
- **A mention mirrored into another entity's visit carries nothing after the mentioned entity's Stop** (Codex review 3
  #2, design T9): the join and the mirrored turn run under the source's fence beside the visited entity's own, and
  the visit door re-checks every acting fence under the visited home's writer lease right before it runs anything.
- **The authoring drainer never runs after a Stop it was admitted before** (Codex review 3 #4): its fence is
  admitted before the paused check, the lease and the runtime lookup, revalidated under the lease, the runtime it
  drains through is the one admitted at that generation, and its writes run under the fence; a Stop refusing a write
  ends the drain as `stopped`, never an authoring failure.
- **A hosted chat opened across a Stop is never left live** (Codex review 3 #8): its registration is one locked step
  with the check of the visit's original admission; a session refused there is disposed (home closed, lease released,
  nothing restored over the Stop), and a Stop during the pending look-back is a refusal, never a failure line.
- **A busy visited home never holds up a mentioned entity's Stop** (Codex review 3 #3): the mirror waits for the
  visited home's writer lease outside every lifecycle lock (the wait ends on the mentioned entity's Stop); only the
  brief check-and-join runs under its lifecycle lock.
- **An unreadable loop status is never taken for a stopped loop** (Codex review 3 #9): a status file that exists but
  is denied or malformed is `failed loop_unverified` on the Stop's receipt (incomplete, a repeated Stop retries), and
  nothing is signalled; only a missing file reads as no loop.
- **Visit doors keep one admission and serve arrivals oldest first** (Codex review 3 #6): the generation read at a
  door's arrival runs through every wait and retry of its open (checked on each wait's exit and again once the open
  lock is held), so a Stop followed by a Resume during a wait still ends the open; and a door takes a ticket on
  arrival and opens only at the head of its home's line (`entity_visits.ARRIVALS`, shared by the durable visit and the
  hosted chat), so a newcomer never overtakes a visitor already waiting. The same visitor rejoining his own live visit
  takes no ticket.
