# Models & engines follow-ups (after WS4b)

**Status**: proposed · **Created**: 2026-09-23 · **Package**: abstractgateway
**Related**: mission "dead-simple install & model management". WS4b delivered the
`/api/gateway/host|engines|models|jobs` mirrors over the Runtime `config_facade`,
`allow_engine_install`, the `abstractgateway models|engines` verbs, and the
Models / Engines console tabs (AbstractCore's embedded screens).

## Deferred items

1. **Console toggle for `allow_engine_install`.** The knob is set through
   `POST /api/gateway/admin/runtime-config` only. An admin on a remote gateway
   should be able to turn it on from the Engines tab (with the same host
   warning the install dialog shows) and see its source (`default`/`stored`).
2. **Where host jobs are persisted.** Gateway jobs run in AbstractCore's default
   job registry, which snapshots jobs under `$ABSTRACTCORE_JOBS_DIR` or
   `<AbstractCore config dir>/jobs` (shared with the `abstractcore` CLI on the
   same account). A gateway service running as another user, or several
   gateways on one account, share or split that list accordingly. Decide
   whether the gateway should pin the registry to `<gateway data dir>/jobs`
   through a facade call (needs a small Runtime facade addition).
3. **A worker that never started keeps `message: "queued"` in Core.** The
   gateway's legacy `/models/download` view shows `error` as the message in
   that case; AbstractCore's `HostJobRegistry.start` should set the message
   itself (AbstractCore backlog).
4. **Streaming CLI output.** `abstractgateway models download` / `engines
   install` poll `/jobs/{id}` once a second and print progress lines; they do
   not stream NDJSON like `abstractcore … --json`. Add `--json` streaming if a
   machine consumer needs it.
5. **`/host/profile` for remote gateways.** Disk paths in `host_profile_v1` are
   the gateway host's; the console already says actions run "on host X".
   Consider hiding absolute paths from non-admin principals (the runtime-config
   path-redaction rule).
6. **Engine install on a service-managed gateway.** A LaunchAgent/systemd
   gateway has a minimal PATH; `brew` may not be found even when installed.
   The install plan's argv uses bare `brew`/`winget`; verify on a real service
   install and, if needed, resolve absolute tool paths in AbstractCore.
