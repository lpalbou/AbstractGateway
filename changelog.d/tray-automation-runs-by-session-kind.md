### Fixed

- **The menu-bar icon no longer lists automation runs as conversations** (operator report 2026-11-03:
  Conversations ▸ Running (2) showed two automations' runs, "You are an expert journalist…" and "you are an
  expert at identifying…", while the same automations sat under Automations). `GET /host/runs` now serves the
  runtime's `session_kind` and `automation_id` on each row (`admin_runtimes.py`; the run index already held
  them), and the tray sorts its rows by that tag alone (`tray/menu_model.is_automation_run`): an automation's
  active run is listed under **Automations ▸ Running (N)**, never under Conversations or its Recent tally,
  and the header line counts them apart ("1 conversation running · 2 automations running"). The classic
  menu lists them under "Automations running (N)". A wake running in its caller's conversation is tagged
  `chat` and stays a conversation.
