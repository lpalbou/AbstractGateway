### Fixed

- **Stopping or restarting the gateway never fails a run** (backlog 1103; `model_host.py`, `runner.py`,
  `host_control.py`, `engines_install.py`). Since the model host became the default (gateway #28, #74), a polite stop
  (SIGTERM, the tray or console Restart, an update, a network change) stopped the model host while a run's model call
  was in flight. The call failed with 424 ("the model host stopped while answering"), the run was marked failed, an
  automation used a retry, and an `automation_failed` email went out. Now:
  - when the gateway stops the model host, or restarts it on purpose (a llama.cpp update), the call in flight is
    interrupted (AbstractRuntime's `EffectInterrupted`), not failed. No incident is written, the run stays running
    at its last durable step, and the call is sent again when the gateway is back. No retry is used and nothing is
    notified;
  - a call made during a restart the gateway asked for waits for the new model host;
  - once a stop begins, no new workflow step starts, and the drain waits only for the step each run is executing;
  - the shutdown line says "runner drain TIMED OUT (…): N run(s) interrupted; they continue at the next start" instead
    of "done" when runs were still executing;
  - a model host that dies on its own (a crash, the memory ceiling, unresponsive) still fails the request with 424
    and an incident; its sentence names the incident file only when one was written for that exit (it used to
    point at `<data dir>/incidents` even when nothing had been recorded).

  AbstractRuntime floor 0.17.0 (`EffectInterrupted`).
