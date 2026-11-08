### Added

- **A gateway iteration default, `agents.max_iterations`** (operator 2026-11-08). How many cycles an agent run may take when
  the app sets no limit of its own: a whole number of 1 or more, unset by default (each workflow's own limit applies, 20 for
  the built-in agent). Read and written on `GET/POST /api/gateway/admin/runtime-config` (`{value, source: stored|default}`;
  a bool, a decimal or a word is refused with a sentence; no upper limit, a `warning` line past 200) and with
  `abstractgateway config set|get|unset agents.max_iterations`; the web console shows it as *Iteration limit* under
  Workflows → Settings (saved, then read back). Every app reads it from `GET /discovery/capabilities`
  (`agent_limits.max_iterations`).
- **`limits` on every `POST /runs/start` answer**: `{max_iterations, source: request|gateway_default|builtin}`, also kept on
  the run as `input_data._gateway_limits`. The app's value wins, else the gateway default is written to
  `_limits.max_iterations`, else the workflow decides. `POST /backlog/maintain` and `POST /backlog/advisor` follow the same
  ladder with their former built-in 12 and 10 and return `limits` too.
