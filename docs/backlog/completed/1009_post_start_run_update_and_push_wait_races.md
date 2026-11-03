# 1009-gateway: [BUG] Two product races behind flaky tests: the post-start run update and the push dispatcher's wait

> Package: abstractgateway
> Type: bug (two product races)
> Status: completed 2026-11-03 (salvaged from PR #22, unreleased branch `release/0.17.3`, where it was numbered 0966)
> Created: 2026-10-28
> Priority: P1 (each failed CI matrix jobs of unrelated PRs)
> Labels: tests, flaky, push, runs, runner
> Root link: AbstractFramework release-closing wave 2026-10-28; release wave 2026-11-03 (PR triage: salvage #22)
> Number: 1009 (0966 is claimed by PR #52)

## The two races

1. `tests/test_gateway_history_bundle_endpoint.py::test_history_bundle_endpoint_includes_snapshot_and_replays_after_restart`
   (`workflow_snapshot` None). **Product race** (`hosts/bundle_host.py`). After `runtime.start` the run is runnable;
   the host then loaded it, added the snapshot ref (and, without a session, the session id) and saved it, while the
   runner's tick (which holds the run's mutation lock for its whole pass) saved its own copy: one save overwrote the
   other (the ref lost, or a finished run back to its first state). Fix: those load-modify-saves run under
   `run_mutation_lock(run_id)` (`_update_run_under_its_lock`) on a short-lived thread; the start waits at most
   `POST_START_UPDATE_WAIT_S` (0.5 s), so a tick already running delays the update, never the start's answer.
2. `tests/test_push_forwarder_setting.py::test_a_split_runner_sees_the_settings_file_move` (timeout). **Product
   race** (`push/dispatcher.py`). The dispatcher read the forwarder setting and the registrations, then started its
   wait with change tokens taken THEN, so a save landing between the read and the wait was already inside the token
   and the wait never ended. Users hit it the same way (a settings save during a pass, a split runner). Fix: the
   tokens are taken BEFORE each read (`_settings_seen`, `_devices_seen`) and the wait compares against those
   (consumed once; a value the pass did not read is taken fresh, so no busy loop).

The 0.17.3 branch also scoped a benchmark's `os.stat` counter to its own thread
(`test_runner_change_token_gate.py`, a harness defect); that change is not part of this item.

## Evidence

- `test_the_snapshot_ref_is_written_under_the_runs_mutation_lock`: another thread taking the run's lock is blocked
  while the ref is written; red with main's `bundle_host.py`, green with the fix (2026-11-03).
- `test_a_setting_saved_between_the_read_and_the_wait_is_not_missed`: saves right after the idle read and right after
  a pass; red with main's `dispatcher.py` (timeout), green with the fix (2026-11-03).
