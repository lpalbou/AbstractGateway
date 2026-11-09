### Fixed

- **A gateway restart is not a Stop** (`entity_phase.resume_after_restart`, operator decision D5, 2026-11-09).
  At the gateway's start, once per entities directory per process and in its own thread, every home whose
  personal grant is still armed and whose own-time loop is not running gets its own time back through
  reconcile (cause `gateway_restart`): the phase to return to is own time, and the loop the restart took
  down (a graceful stop before it, or a pid gone with it) is not refused as `loop_crashed`. A visit, a
  sleep or work standing at the start keeps the phase, and own time resumes when it ends. Never for a
  stopped home, never for an archived one, never from idle without a grant. The restore is recorded in
  the phase sidecar as `restart_resume {generation, at}` and holds only under that generation (a Stop
  cancels it); it is spent once a start is attempted. Off with the other background entity passes
  (`ABSTRACTGATEWAY_ENTITY_SELF_REPAIR=0`). Before: nerin needed a manual Retry after every restart.
