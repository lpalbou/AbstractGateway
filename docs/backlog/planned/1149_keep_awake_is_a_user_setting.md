# 1149-gateway: [FEATURE] "Keep the Mac awake while the gateway runs" is a user setting (web console, terminal console, tray)

> Package: abstractgateway (keep_awake.py, settings store, console.py + console_ui.py, console-tui, tray menu)
> Type: feature
> Status: planned
> Created: 2026-11-08
> Priority: P1 (operator: part of the current release; no separate release for it)
> Labels: settings, tray, console, keep-awake

## Operator ruling (2026-11-08)

"Any option such as 'prevent to sleep' should be governed by the user. I would put it in the gateway console WUI + TUI,
possibly even in the systray of the gateway, e.g. in Settings" (the tray's Settings submenu today: Start at login, Warn
when memory is almost full, Classic menu).

## Current code reality

`keep_awake.py` holds the macOS `PreventUserIdleSystemSleep` assertion (what `caffeinate -i` holds), named
"AbstractGateway", from `serve` start to stop (`cli.py:666-686`), unconditionally, with no switch and no message
(ruling 2026-11-02: "keep-awake = gateway holds idle-sleep assertion while running, no switches/messages"). The operator
now wants it governed by the user; the 2026-11-02 ruling is superseded by this one.

## Scope

### In scope

- A stored setting `keep_awake` (default ON, so behaviour is unchanged for existing installs), read at serve start and
  changed live: turning it off releases the assertion at once, turning it on holds it again (no restart).
- Surfaces, one rule each, the same words everywhere: web console (System or Network page, a real switch like "Start at
  login", admin only, verified by GET), terminal console (same row), the tray's Settings submenu (a checkmark item
  "Keep the Mac awake while the gateway runs"). A launch flag `serve --keep-awake on|off` for one run (no env var).
- `GET /api/gateway/host` (or the existing host/status route) reports `keep_awake: {value, source, held}` so every door
  shows the same truth; `pmset -g assertions` is the independent check.
- Non-macOS: the row says the platform has no such assertion; the setting still stores.

### Out of scope

- Any "wake on network" or scheduled wake; any change to what the gateway does when the Mac does sleep.

## Acceptance criteria

- [ ] Toggle off in the web console: within 1 s `pmset -g assertions` no longer lists AbstractGateway; toggle on: listed.
- [ ] Terminal console and tray show the same state as the web console after each change (and after a restart).
- [ ] Tests: setting store round-trip, live release/hold on change, the route's three fields, tray item state.

## Status 2026-11-08 14:10

Gateway setting, web console switch (beside Start at login) and tray checkmark DONE and live since 13:30 (gateway
9e1947b5, 0d740059). Remaining, next release: the terminal console row (a separate Rust crate re-release).
