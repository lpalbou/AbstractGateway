### Changed

- **The console's top bar shows the kit's one account widget** (`console_ui.py` `topBarIslandProps()`, `console.py`
  `renderAccount`). Signed in, the right end of the top bar is the same control as in the other apps: a person icon,
  the account name and a status light. Its menu shows "Signed in as …", the gateway address, Copy user id and
  Disconnect. The console passes `identity: "menu"` and its own `GET /api/gateway/me` body (`connection.me`) to the
  kit island. The "Signed in as" text beside the connection pill is gone. About stays the console's own dialog for
  now, because it carries the update controls (backlog 0976).
- **The vendored kit islands are ui-kit 0.9.6** (AbstractUIC 0.8.5; `console_islands.py`, and the theme provenance in
  `console_themes.py`), regenerated from the released tag. Before this, they were 0.9.4 (backlog 0974).
