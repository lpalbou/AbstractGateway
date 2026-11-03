# 0934 — `abstractgateway service uninstall` must wait for the whole gateway process tree to exit

**Status**: completed 2026-10-29 (unreleased) · **Priority**: P1 · **Created**: 2026-09-27
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

## Completion 2026-10-29

- `process_tree.py` (new) captures the gateway's tree BEFORE `bootout` or `disable --now`, while every
  detached child still has the serve process as its parent. The tree is the recorded pids
  (`run/gateway-serve.json`, `run/apps/*.json`), each trusted only when it is alive, belongs to this user and
  its command line names an abstract* program; plus the processes running this data folder's app watcher;
  plus all of their descendants. This command and its ancestors are never part of it.
- The uninstall plan gains a step `@stop-gateway-tree <wait>` after the service manager's stop. The step waits
  up to 20 s for the tree to exit (new children of a tracked process are tracked too), then sends SIGTERM,
  then SIGKILL, and reports what was running and how each process ended (`results[].stop_tree`, and lines
  in the text output). The command fails, naming them, when something survives. On Linux XDG autostart
  there is no manager, so the step waits 0 s and stops the gateway itself. `service disable` stops nothing,
  as before.
- `--dry-run` prints "stop: the gateway's processes and the children it started (wait up to 20 s …)".
- No pid registry was needed: the snapshot before the stop covers the detached children. A gateway run from
  somewhere else is still stopped when its serve record is in this data folder.
- Tests: `tests/test_gateway_service_uninstall_tree.py`. A real fake serve process has a detached child
  that ignores SIGTERM, and launchd is a stub whose bootout sends SIGTERM. Both processes are gone and
  reported. The XDG case, the survivor failure, the tree rules and the dry run are covered too.
  Mutation-checked: a snapshot taken at step time misses the child, and a survivor that does not fail the
  command turns the test red.
