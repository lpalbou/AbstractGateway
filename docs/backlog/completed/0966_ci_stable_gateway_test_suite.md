# 0966-gateway: [BUG] CI-stable gateway test suite: three more flaky tests, two of them product races

> Package: abstractgateway
> Type: bug (two product races, one harness defect)
> Status: fixed on branch release/0.17.3, NOT released (paused 2026-10-28; target 0.17.3)
> Created: 2026-10-28
> Priority: P1 (each failed a CI matrix job of a docs-only PR: run 40739931377)
> Labels: tests, flaky, push, runs, runner
> Root link: AbstractFramework release-closing wave 2026-10-28 (coordinator: a check fails only for a real defect; no admin merges)
> Number: 0966 (0964-0965 are docs items of PR #20). The fourth flaky test is [0963](0963_flaky_archive_test_races_the_boot_index_warm.md).

## The three tests, cause and verdict

1. `tests/test_push_forwarder_setting.py::test_a_split_runner_sees_the_settings_file_move` ("condition not met in
   time", test 3.12 job 122998624859). **Product race** (`push/dispatcher.py`). The dispatcher read the forwarder
   setting (`refresh_relay`) and the registrations (`_devices`), then started its wait (`_wait_active` / `_park`)
   with change tokens taken THEN. A save landing between the read and the wait was already inside the token, so
   the wait never ended: the test saved "off" right after the relay call, while the dispatcher was still
   finishing that pass. Users hit it the same way (a settings PUT during a pass, a split runner). Fix: the tokens
   are taken BEFORE each read (`_settings_seen`, `_devices_seen`) and the wait compares against those (consumed
   once; a value the pass did not read is taken fresh, so no busy loop).
2. `tests/test_gateway_history_bundle_endpoint.py::test_history_bundle_endpoint_includes_snapshot_and_replays_after_restart`
   (`workflow_snapshot` None, test 3.10 job 122998625170). **Product race** (`hosts/bundle_host.py`). After
   `runtime.start` the run is runnable; the host then loaded it, added the snapshot ref (and, without a session,
   the session id) and saved it, while the runner's tick (which holds the run's mutation lock for its whole pass)
   saved its own copy: one save overwrote the other (the ref lost, or a finished run back to its first state).
   Fix: those load-modify-saves run under `run_mutation_lock(run_id)` (`_update_run_under_its_lock`) on a
   short-lived thread; the start waits at most `POST_START_UPDATE_WAIT_S` (0.5 s) for it, so a tick already
   running delays the update, never the start's answer.
3. `tests/test_runner_change_token_gate.py::test_benchmark_11500_quiet_runs_probe_under_1ms_and_reads_no_file`
   (`assert 202 <= 200`, test 3.13 job 122998625084). **Harness defect.** Not the wall-clock budget (that is
   already a non-asserting report outside `ABSTRACTGATEWAY_TIMING_STRICT=1`): the file-access counters patched
   `os.stat` process-wide, so another thread's stats (a thread left by another test, a store's background
   reconciliation) counted as the probe's. Fix: only the probing thread's calls count; the test now runs a
   stat-ing thread beside the probe on purpose. "Reads no file" stays strict.

## Evidence

- (1) `test_a_setting_saved_between_the_read_and_the_wait_is_not_missed` saves right after the idle read and right
  after a pass: red with the previous `dispatcher.py` (timeout), green now.
- (2) `test_the_snapshot_ref_is_written_under_the_runs_mutation_lock`: another thread taking the run's lock is
  blocked while the ref is written; red with the previous `bundle_host.py` and with the lock removed from the helper.
- (3) with the counter unscoped again the test reads `202 <= 200`, the CI's exact failure; green scoped.
- The four flaky tests (with 0963's), 20 consecutive runs each, plain and beside 12 CPU-busy processes: 0 failures
  (framework root `untracked/release-closing/w5/round2/flaky20.log`, `flaky20.sh`).
- The gateway suite also showed `tests/test_mail_collector_change_driven.py::test_400_finished_runs_a_change_to_one_loads_exactly_one`
  red with AbstractRuntime 0.15.2 (`assert 400 == 0`): its index-build replay signalled every recent run as changed.
  Fixed in AbstractRuntime 0.15.3 (its backlog 0864), now the floor.
