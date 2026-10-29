# An installed browser app older than a release's pin is named (console card, boot log)

**Status**: completed 2026-10-29 (unreleased; ships with the next AbstractGateway release).
**Found by**: the OVH release-test server: a reopened conversation in AbstractCode showed its last turn only.
The ledger, the replay view and the session's turn listing were intact; the served Code was 0.13.0
(AbstractFramework 0.11.17's pin), which predates AbstractUIC panel-chat 0.6.2's `listSessionTurns`. Code 0.13.3
(updated on the server) shows every turn after a reload; nothing told the operator the app was behind.

## Change

- `self_update.check_for_update` keeps the newest AbstractFramework release's `npm_apps` pins
  (`newest_release_app_pins()`, no extra request).
- `apps_manager.app_older_than_pin()`: one readable sentence for an installed app older than this install's
  release pin, else than the newest release's pin ("Code 0.13.0 is older than the 0.13.2 that AbstractFramework
  0.11.18 installs: update Code to get its fixes."); `None` otherwise. Each browser app row carries it as
  `pin_warning`; the console's Apps card shows it as a warning line (it asks for an action: Update).
- Gateway boot: after the enabled apps start, one `[WARN]` line per app behind a pin. Boot makes no network
  request: it reads this install's own pins and the newest release from the last update check, if one ran. The
  first version ran `check_for_update()` at boot. The merge desk caught that `GET /api/gateway/update` then found
  a check nobody asked for (`test_tray_and_update_routes` was red), so the console row says the rest once a check
  has run.

## Tests

`tests/test_gateway_apps_manager.py::test_an_app_older_than_a_release_pin_is_named_in_its_row_and_at_boot` (it also asserts that boot runs no update check, red with the check put back),
`::test_the_update_check_keeps_the_newest_release_app_pins`; red when the newest-pin comparison is removed.
