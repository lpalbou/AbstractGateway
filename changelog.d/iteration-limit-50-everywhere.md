### Changed

- **The iteration default is 50, read from AbstractRuntime** (operator 2026-11-08: "By default, set it to 50
  everywhere"). Unset `agents.max_iterations` now means the framework default, `DEFAULT_MAX_ITERATIONS` (50), imported
  by name: the runtime-config read carries `default: 50`, `capabilities.agent_limits.max_iterations.default` is 50
  (`source: "default"`), a `POST /runs/start` without a value answers `limits: {max_iterations: 50, source: "builtin"}`
  (it was `null`), and `POST /backlog/maintain` / `/backlog/advisor` lose their own 12 and 10. The console field reads
  "Not set: the framework default (50)" and its clear button is *Use the default*.

### Fixed

- **The requested limit reaches the built-in agent's loop.** The basic-agent's On Flow Start pin default (20) used to
  replace a `_limits.max_iterations` that a run asked for (the gateway default, the kit field) in the agent's child
  run; with AbstractRuntime's fix the requested value governs the whole run tree (`tests/test_gateway_iteration_limit_tree.py`
  runs the real loop: 50 asked = 50 cycles, 10 = 10, none = 50). Needs the AbstractRuntime release that carries
  `DEFAULT_MAX_ITERATIONS`.
