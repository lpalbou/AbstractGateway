### Fixed

- **A fresh install stores the iteration limit 50** (operator decision D2, 2026-11-09: "50 everywhere").
  The limit ladder puts a workflow's own budget above the framework default, and the shipped basic-agent
  and goal-agent bundles bake 20, so on a fresh install their runs got 20; the live 50 was a setting
  stored by hand. `serve` on a fresh data dir (first run not completed, nothing stored for the key) now
  stores `agents.max_iterations = 50` through the console's own settings door, once
  (`runtime_config.seed_fresh_install_defaults`, recorded under `seeded_defaults`). An existing install,
  a stored value and a later clear are never touched.
- **Next wave:** the basic-agent and goal-agent bundle builders still bake 20 into their On Flow Start pin;
  until they bake 50, an install whose first run predates this release and that never stored the setting
  keeps 20 for those bundles (`abstractgateway config set agents.max_iterations 50` fixes it).
