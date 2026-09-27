# 0934 — `abstractgateway service uninstall` must wait for the whole gateway process tree to exit

**Status**: planned · **Priority**: P1 · **Created**: 2026-09-27
**Package**: abstractgateway · **Related**: abstractframework backlog 0934 (uninstall purge race — the installer now stops the tree itself),
abstractruntime `identity/life.py:1896-1904`, abstractcore `config/host_jobs.py:1620`

## Why this is here

The framework uninstaller failed on an operator machine at `rm -rf <data dir>` with "Directory not empty": `service uninstall` runs
`launchctl bootout` once and returns (`src/abstractgateway/os_service.py:873-879`, executed by `execute_plan` `:927-994`) without waiting
for the serve process to exit, and several children are started in their own session (`start_new_session=True`) so launchd never stops
them: the entity own-time loop (writes `<data>/entities/…/own_time.log`), model download jobs, maintenance-manager processes
(`maintenance/process_manager.py:803`), the apps the gateway starts (`apps_manager.py:951`, `apps_desktop.py:436`), and the tray
(`tray_supervisor.py:338`, exits only after its parent). They keep writing into the data dir for seconds. The installer (root `dee84f9`)
now stops the tree itself, but the command's own contract ("stops it and removes the login entry") is not honoured: anyone calling
`service uninstall` directly, or a Linux XDG-autostart gateway (`os_service.py:880-890` never stops it), gets a half-stopped gateway.

## Scope

- After `bootout` (macOS) / `disable --now` (Linux), read `<data>/run/gateway-serve.json`, wait for that pid and its descendants (and
  the detached children the gateway itself started: keep a registry of their pids under `<data>/run/`, or tag them by cmdline) up to a
  bound, then TERM, then KILL; report what was stopped; exit non-zero if something survives, naming it.
- Linux XDG autostart: stop the autostarted gateway too.
- Out of scope: deleting data (the installer's job); Windows.

## Validation

A test that starts a fake serve process with a detached child ignoring SIGTERM, runs `service uninstall` against a scratch data dir with
launchd/systemd calls stubbed, and asserts both are gone and the command reports them; the installer's tree stop then finds nothing to do.
