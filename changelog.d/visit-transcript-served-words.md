### Fixed

- **A visit's transcript shows what the visit served** (Castor E2E 2026-11-10). `GET /entities/{name}/visit/{run_id}/transcript`
  (what the Entity app reads on reload) served the words as SENT: the visitor's message with the whole MEMORIES
  prompt around it, and the reply with its raw election markers (`[learned: …]`). It now serves, from the visit
  run's own ledger, the visitor's words as he sent them and the reply as the turn's answer served it, with that
  answer's `notices` (what the turn formed or staged) and its `turn_id`. What he attached stays in `attachments`
  (the "(attached: …)" lines were the prompt's decoration); a failed turn keeps its failure sentence.
