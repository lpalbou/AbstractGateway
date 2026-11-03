### Fixed

- **The menu-bar icon no longer lists automation runs as conversations.** `GET /host/runs` serves the runtime's
  `session_kind` and `automation_id` on each row (`admin_runtimes.py`), and the tray sorts its rows by that tag
  (`tray/menu_model.is_automation_run`): an automation's active run is listed under **Automations ▸ Running (N)**,
  never under Conversations or its Recent tally, and the header line counts them apart ("1 conversation running ·
  2 automations running"). The classic menu lists them under "Automations running (N)". A wake running in its
  caller's conversation is tagged `chat` and stays a conversation.
