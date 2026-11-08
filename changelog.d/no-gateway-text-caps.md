### Changed

- **No gateway text cap refuses a title any more** (`automation_defaults.py`, `events_stream.py`,
  `admin_runtimes.py`; root ADR 0054, operator 2026-11-08 — the runtime removed its own stored-text caps the same day).
  A visual flow's `automation_defaults.title` was refused past 120 characters and a flow's published event `title`
  past 200: both are now stored whole (a title is a non-empty, trimmed string; nothing else). The desktop tray's
  Sessions rows keep their 60-character fold as a display choice: `GET /host/runs` rows carry `title` folded with a
  visible "…" and the whole first line beside it as `title_whole`, so nothing is cut silently. A published event's
  `body` (was refused past 4,000 characters) and `subject` (past 200) are stored whole too: text is the only rule
  (ADR 0026/0054: stored text has no length caps).
