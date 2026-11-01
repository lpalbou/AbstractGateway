### Changed

- **The tray menu is redesigned** (operator 2026-11-01: "not user friendly, not organised, repetitive";
  `tray/menu_v2.py`, docs/tray.md). It reads top to bottom in the order a person thinks: a header — on macOS a
  native row (`tray/header_mac.py`, `tray/logo.py`): the AbstractFramework logo, the name, what the machine is doing
  in words (`2 conversations running · 1 needs you · 1 model loaded`) and a switch that is ON while conversations run
  and OFF while the gateway is paused (the gateway-wide pause); Windows and Linux show the same as two text lines —,
  the meters as percentages, the two doors (**Open Console**, **Open Assistant** promoted
  from Apps ▸), **Needs you** — every approval or question as its own top-level row with Show and approve… / Deny /
  Open in Observer (at most three, then one line for the rest) —, then **Conversations ▸** (Running, Need attention, Recent), **Automations ▸** (Active, Paused, the Observer
  page — automations are standing orders, not conversations), **Models ▸** (memory first: the full meters, one
  **Eject X (size, engine)…** row per loaded model, **Eject All — frees N…**, then Load a Model, the Activity window
  and the Resources tab), **Apps ▸**,
  **Connect ▸** (Addresses ▸ and Network ▸ merged: where it answers now, the addresses to copy, who can reach it,
  the restart that applies it), **Settings ▸** (Start at login, Warn when memory is almost full, Classic menu),
  **Help ▸** (links, Copy Console Link, Check for Updates… while no update is known, About), and the lifecycle rows
  (an update's state while one applies, ⏸ Pause / ▶ Resume, Restart…, Quit AbstractGateway…). The product name is on
  the header and on Quit only. Same `Node` model, same action tuples and handlers as before: every classic action is still
  produced (`tests/test_gateway_tray_menu_v2.py`), `flat_menu` applies, nothing is dropped.
- **The classic menu is kept whole for a roll-back** (`menu_model.py`, untouched): **Settings ▸ Classic menu**
  (remembered in `<data_dir>/tray/prefs.json` `menu_style`; the classic menu's **Help ▸ New menu** is the way back),
  or `abstractgateway serve --tray-menu classic|v2` for one run (the flag wins over the preference).
