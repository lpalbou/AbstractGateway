### Fixed

- **A start runs under the admission it was decided at** (AbstractRuntime d47b7a2: `spawn_loop_process(fence=)`
  is required). The operator's `loop/start`, the restore of own time after an interruption or a restart
  (`reconcile`) and the unattended need-check's wake admit the home BEFORE their checks and hand that fence to the
  spawn: a Stop landing after the decision spawns nothing (`loop/start` answers 409 `stopped_use_resume`).
- **A home with no lifecycle signal is admitted once** (AbstractRuntime 3cd872b: a missing signal reads as moved,
  fail closed). Every fence the gateway builds from a remembered generation (`fenced()`, `admission_fence`, a
  runtime readmitted for new work) first admits a home whose signal was never written, so homes that predate it
  are not refused every write; `admission_fence(None)` is the runtime's `LifecycleFence.admit`.
- **The shared home's book append holds the home's write barrier** (AbstractRuntime d3ffc65: `DiaryStore(guard=,
  barrier_home=)`): a diary write whose fence check passed lands before a racing Stop's generation bump.
