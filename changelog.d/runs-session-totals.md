### Added

- **`session_totals` on `GET /api/gateway/runs`** (`routes/gateway.py`; operator 2026-11-08). Code's sidebar said
  "≥4 turns" because a listing page holds a few runs of a session, never all of them. Every listing page now carries
  `session_totals`: for each `session_id` on the page, `{"turns": <turn roots in the whole session>, "runs": <all its
  runs>}`, under the same `session_kind` filter as the page (a conversation listing counts conversation runs), read from
  the file store's session and children indexes — no run file and no ledger is read, and only the sessions on the page
  are counted (measured on an 11,500-run, 2,114-session store copy: 5.4 ms median for a page naming 50 sessions,
  about 10 ms for 200, on top of the index the gateway already builds at boot). The key is absent when the page names
  no session, or when the run store is not the file-backed one (no such index: nothing is guessed).
