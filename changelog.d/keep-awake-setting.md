### Added

- **"Keep the Mac awake while the gateway runs" is a user setting** (`keep_awake.py`, `runtime_config.py`,
  `routes/keep_awake.py`, the web console's Gateway card, the tray's Settings submenu; backlog 1149, operator
  2026-11-08 — the 2026-11-02 "no switch" ruling is superseded). The idle-sleep assertion the gateway holds on macOS
  is now governed by the stored setting `host.keep_awake` (default on, so existing installs behave the same), read at
  serve start and changed live: off releases the assertion at once, on holds it again, no restart.
  `serve --keep-awake on|off` sets it for one run (no environment variable; the saved value applies at the next
  start). `GET /api/gateway/host/runner` carries `keep_awake: {value, source: flag|stored|default, held,
  platform_supported[, stored_value]}`; `PUT /api/gateway/host/keep-awake {enabled}` (admin) stores it, applies it
  and returns the same shape. The web console shows a switch beside "Start at login" on the Gateway card with the
  same words as the tray ("Keep the Mac awake while the gateway runs"; pressed = on), verified by a fresh read after
  every click; the tray's Settings submenu has the check item right under "Start at login", bound to the same
  route. On a system without such an assertion the row says so and the setting still stores. The terminal console's
  row is a separate crate and comes later. Independent check on a Mac: `pmset -g assertions` lists `AbstractGateway`
  under PreventUserIdleSystemSleep while held.
