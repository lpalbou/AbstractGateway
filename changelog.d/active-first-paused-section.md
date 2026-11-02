### Added

- **Automations are listed active first, then paused, each by last activity** (`automation_order.py`,
  framework ADR 0048). `GET /api/gateway/automations` serves the order every app draws: automations that
  are neither paused nor archived, most recent activity first, then paused ones the same way. Each summary
  (the listing and `GET /automations/{id}`) carries `section` (`active`, `paused` or `archived`) and
  `last_activity_at` (the automation's last change), and every page counts `paused_automations` beside
  `archived_automations`. Older scheduled runs are placed by the same rule instead of closing the last
  page. A cursor from an earlier gateway is refused with 422 `invalid_request`; start from the first page.
- **The tray folds paused automations into a "Paused (N)" submenu**, in the gateway's order, in both the
  Automations and the Sessions menus.
