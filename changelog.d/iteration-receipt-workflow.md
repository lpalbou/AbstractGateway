### Fixed

- **The iteration limit follows one order and the receipt says which rung applied** (`routes/gateway.py`,
  `hosts/bundle_host.py`, `runtime_config.py`; review 2026-11-09 finding 2). Order: the person's request > the stored
  `agents.max_iterations` > the workflow's own value > the framework default (50). A request (the `_limits` field or
  the flat `max_iterations` pin) and the stored default are written to `_limits.max_iterations`, which AbstractRuntime
  treats as declared: it now beats an Agent node's own pin default or config (a request of 100 used to run 20). With
  neither, the workflow's own value applies (an On Flow Start pin default, an Agent or subflow node's value) and the
  receipt says `{"max_iterations": <value>, "source": "workflow"}` (`null` when the workflow's nodes author several
  values), else `{50, "builtin"}`; `workflow_iteration_limit(visualflow)` reads it the way the runtime applies it. A
  client-sent `_limits.max_iterations_declared` is dropped. The shipped basic-agent authors 20 on its start pin, so with
  nothing requested and nothing stored it now runs 20 (store `agents.max_iterations` to set a gateway-wide default).
  Requires AbstractRuntime with `declared_max_iterations`.
