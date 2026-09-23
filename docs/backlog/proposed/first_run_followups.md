# First-run follow-ups (after WS4a)

**Status**: proposed · **Created**: 2026-09-23 · **Package**: abstractgateway
**Related**: mission "dead-simple install & model management" (WS4a delivered the
loopback auth default, claim links, first-run guide, `service install`, per-OS
data folder, Windows runner lock).

## Deferred items

1. **Validate `service install` on real machines.** Unit tests render every OS's
   artifact; a real `launchctl bootstrap`, `systemctl --user enable --now` and
   the Windows Startup shortcut (PowerShell COM + `pythonw`) have not been run.
   Windows is marked experimental until a Windows VM run passes. Also confirm
   whether `Register-ScheduledTask -AtLogOn -User $env:USERNAME` works without
   elevation (a possible replacement for the shortcut).
2. **Other `fcntl` users on Windows.** `entity_tasks.py`, `tool_grants.py` and
   `entity_replay.py` still import `fcntl` directly; only the runner singleton
   lock got the `msvcrt` branch.
3. ~~**Engines step.**~~ Done in WS4b (branch `feat/inherit-core`): the gateway
   mirrors `/engines`, `/models/*`, `/jobs/*` and the wizard mounts the
   AbstractCore Engines and Models screens (see `models_engines_followups.md`).
4. **App sign-in without copying a token.** The npx apps still need a Gateway user
   token. A per-app claim/device flow would remove the last copy/paste step.
5. **`abstractgateway-config init`** still defaults `--data-dir ./runtime/gateway`
   and writes an `ABSTRACTGATEWAY_AUTH_TOKEN`; align it with the first-run
   defaults or document it as the server/operator path only.
6. **Service stop on Windows uninstall.** Uninstall removes the shortcut but does
   not stop a running gateway; use the serve record pid to stop it.
7. **Doctor consumer.** The root `abstractframework doctor` should read
   `abstractgateway-config status --json` (schema `gateway_config_status_v1`).
