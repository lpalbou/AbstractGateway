### Changed

- **A dev server an agent starts lives as long as its conversation** (operator decision 2026-11-09; needs
  AbstractRuntime 85a536b). AbstractRuntime now keeps a `local_helper_start` helper for the session instead
  of closing it when the turn's run ends, so a server started in one Code turn still answers in the next.
  The gateway ends it with the conversation: `POST /sessions/{id}/archive` stops the session's helpers
  (`helpers_stopped: N` in its answer), and the gateway's stop stops every helper (shutdown stage
  "local helpers stop"); the agent or the operator can still stop one (`local_helper_stop`).
- Known gap: with a separate runner process (split mode) the helpers live in the runner, and an archive
  answered by the HTTP process does not reach them; they stop with the runner.
