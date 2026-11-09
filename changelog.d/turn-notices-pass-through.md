### Changed

- **A turn's notices are served beside its reply, unchanged** (AbstractRuntime 09cbad7, the Entity app
  owner's contract 2026-11-09: `[{kind, record_id, title, text?, staged?, visibility?}]`; the served reply
  carries no bracket markers, the stored words keep them).
  - The visit door answers the turn's served reply (`_turn.reply`), no longer the visit history's stored
    words, and its probe carries `reply_notices` (`notices` stays the operator's log lines).
  - The visit close answers `notices`: each election the visit's turns staged (lesson, interest,
    realization) that the close formed, with the formed record's graph id, read from the run's own ledger,
    so a "forming" badge resolves to its record.
  - The hosted chat turn answers `reply_notices` beside `reply`.
  - A mention answers `notices` and records them on its session turn (`output.notices`, read by the session
    turn rows of history bundles); a mirrored turn's row carries the visited entity's `notices`.
- Needs the AbstractRuntime release that carries 09cbad7. The visit run's own session row gets no close
  notices until the runtime's visit output names them.
