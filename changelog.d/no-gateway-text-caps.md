### Changed

- **No gateway text cap refuses a title any more** (`automation_defaults.py`, `events_stream.py`,
  `admin_runtimes.py`; root ADR 0054, operator 2026-11-08 — the runtime removed its own stored-text caps the same day).
  A visual flow's `automation_defaults.title` was refused past 120 characters and a flow's published event `title`
  past 200: both are now stored whole (a title is a non-empty, trimmed string; nothing else). The desktop tray's
  Sessions rows keep their 60-character fold as a display choice: `GET /host/runs` rows carry `title` folded with a
  visible "…" and the whole first line beside it as `title_whole`, so nothing is cut silently. Still in place and
  named here for the operator: a published event's `body` (4,000) and `subject` (200) length refusals in
  `events_stream.py`.
