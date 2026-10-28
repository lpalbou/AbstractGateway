# 0963-gateway: [BUG] Flaky `test_runs_reads_archive_and_kinds_once_and_sees_its_own_writes` (404 `session_not_found`)

> Package: abstractgateway
> Type: bug (test harness; product race fixed in AbstractRuntime 0.15.2, its backlog 0863)
> Status: completed (2026-10-28, 0.17.3)
> Created: 2026-10-28
> Priority: P1 (it blocked the 0.17.2 PR and release runs)
> Labels: tests, flaky, run-store, index, boot
> Root link: AbstractFramework release-closing wave 2026-10-28 (coordinator ask: no more admin merges on this test)
> Number: 0963

## Summary

`tests/test_perf_routes_off_loop.py::test_runs_reads_archive_and_kinds_once_and_sees_its_own_writes` failed on one
matrix job per CI run (e.g. CI run 40729538098, Python 3.13): `POST /api/gateway/sessions/s1/archive` answered 404
`{"reason_code":"session_not_found"}` for a session whose run the test had just written.

## Cause

The test wrote its two runs with its own `JsonFileRunStore` AFTER `TestClient` started the app, i.e. while the
gateway's boot thread (`begin_gateway_boot` -> `create_default_gateway_service` -> `warm_json_run_store`) was
building the service store's session index. AbstractRuntime's `JsonFileRunStore` appends a new run's `+` journal
line BEFORE publishing its file (J53-1), and an index build marks the journal offset (`_mark_journal_start`) and
then scans the folder. A run whose `+` line lands before the mark and whose file lands after the scan is in
neither: the session index misses it until the full reconciliation (`RUN_INDEX_FULL_RECONCILE_S`, 600 s), and the
archive route's `session_exists` (`list_run_index(session_id=...)`) answers 404. A busy runner widens that window
by chance.

This is a product race in AbstractRuntime (any process creating a run while another process's store builds its
index: a split-mode runner during the API process's boot), not in the gateway: deterministic repro, two store
objects on one folder, the boot warm-up run between the foreign `+` line and its publication -> the file exists,
`list_run_index(session_id="s1")` returns 0 rows (framework root
`untracked/release-closing/w5/round2/race/repro_index_race.py`). Fixed at its source in AbstractRuntime 0.15.2
(backlog 0863: the first sync after an index build re-applies the journal's last 16 KiB before the build's mark).

## What changed

- Floor `AbstractRuntime>=0.15.2` (`pyproject.toml` base, `[apple]`, `[gpu]`; `live_deltas.ABSTRACTRUNTIME_FLOOR`,
  which the install-profile test pins and the startup "runtime too old" message names; that check probes
  features, so only the pip floor keeps a 0.15.1 install out).
- The test writes its runs before the gateway boots (the boot warm-up then indexes them), and checks the first
  listing shows both sessions. What it tests (listings read the archive and kinds files once; the archive write is
  seen at once) is unchanged.

## Evidence

- With the window between the `+` line and the publication widened to 0.3 s
  (`untracked/release-closing/w5/round2/race/widen_window.py`): previous test 5/5 failed with the CI's exact 404;
  fixed test 25/25 passed (5 idle, 20 with 12 CPU-busy processes).
- The previous test order with the widened window against AbstractRuntime 0.15.2: 5/5 passed (3/3 failed with
  0.15.1): the product race itself is closed.
- Fixed test, 20 consecutive plain runs: 20 passed; the whole file 3 times under the same CPU load: 3 passed.
