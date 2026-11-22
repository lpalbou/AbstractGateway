### Fixed

- **An own-time status that cannot be read is said, and nothing is started over it** (`entity_phase.py`,
  `entity_idle_sleep.py`, `entity_loop.py`, `routes/entities.py`). When the read of an entity's `loop_status`
  raised, the phase bar folded it into "His own time is not running. Retry to start it.", and each Retry met the
  same error (on Windows every read raised: `module 'os' has no attribute 'WNOHANG'`, fixed in AbstractRuntime).
  A status nobody can read is unknown, never "not running": the bar now answers `blocked.code
  own_time_status_unreadable` ("His own-time status can't be read (<kind of error>). Nothing was started or
  stopped.", no Retry; the detail is in `warnings`), reconcile and Retry start nothing over it, the idle sweep
  writes no sleep over it, a remembered return to own time is kept, and turning own time off no longer answers
  500: the grant is revoked and the stop is enqueued for a loop that may be there. Stop works as before.
- **One liveness probe for an entity's loop** (`entity_phase._pid_liveness`): the gateway's own copy called
  `os.waitpid(pid, os.WNOHANG)` and `os.kill(pid, 0)`, neither of which works on Windows. It now asks the
  runtime's probe (`abstractruntime.identity.life._pid_state`; needs the AbstractRuntime release that carries
  `utils.os_process` to work on Windows).
