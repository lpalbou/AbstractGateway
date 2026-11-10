### Fixed

- **The ledger names whose act a phase change was** (Castor E2E 2026-11-10). The idle sweep's sleep entered the
  ledger as `cause: operator_sleep`; it is `idle_sleep` (his own sleep, the threshold in its reason). The Stop's
  own row read `cause: visit_closed` when the visit it closed published first; entering `stopped` is always
  `kill_switch`, and the Stop publishes under that cause. The tile treats both as door acts already recorded as
  their own moments (`idle_sleep`, `pause`).
