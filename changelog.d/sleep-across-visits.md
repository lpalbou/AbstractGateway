### Fixed

- **A sleep keeps its reason and its start across visits** (`entity_phase.continue_sleep`). Every visit that
  interrupted a sleep appended "(visitor session ended: N turns; closed)" to the sleep's reason when it closed
  (castor's reason carried sixteen of them) and moved the sleep's start to the close. The same sleep now
  continues unchanged on every lane (the durable visit's close, the hosted chat's close, an aborted open): its
  original reason, who chose it, its wake time, and `since` (both `life_state.since` and `sleep.since`) where
  the sleep began, however many visits came in between. The visits are in the ledger instead: a `summon`
  marker at each open and a `session_closed` marker at each end, which now carries `sleep_continues: true`
  when the sleep goes on. A reason that already grew before this release keeps its old suffixes until the
  sleep ends.
