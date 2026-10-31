### Added

- Account-wide conversation read receipts persist as immutable per-turn request/outcome events.
  Clients replay them through `/api/gateway/me/session-reads`; `session.read` on the personal
  event stream updates other clients. Receipts survive restart and stream retention, remain
  isolated by principal/runtime, and do not modify workflow execution ledgers.
