# A failed start exits non-zero (systemd restarts it); the admin token never lands in a service log

**Status**: completed 2026-10-29 (unreleased; ships with the next AbstractGateway release).
**Found by**: the OVH release-test server (2026-10-28/29). The operator saw the gateway "crash".

## Evidence (journal, `~/.local/share/abstractgateway`)

- 17:37 `install.sh --server` (MODE=background) started a gateway outside systemd on port 8080.
- 17:52 the systemd unit's gateway could not bind (`[Errno 98] address already in use`), shut down cleanly,
  and its straggler belt (`cli._exit_now_if_nondaemon_stragglers`) found an idle off-loop worker (`gw-light_0`)
  and `os._exit(0)`: a failure exited 0, so `Restart=on-failure` never retried. From then on the gateway that
  served was unsupervised.
- 19:29 the host itself went down (user session exit, `system down` until 23:02, `last -x`); the operator's
  "crash" was the host shutdown, and nothing was supervised to bring the gateway back meanwhile. The ledgers of
  the runs before it are intact.
- The journal also held the bootstrap admin token in clear (a loopback gateway prints it to stderr; under
  systemd stderr is the journal).

## Fix

- `cli._pending_exit_code()`: the belt exits with the code of what is propagating (uvicorn's STARTUP_FAILURE 3
  for a port in use, 1 for an exception, 0 for a clean stop or Ctrl-C).
- `work_pools.shutdown_all()`: the off-loop pools are stopped (queued work cancelled, idle workers joined, 2 s)
  before the belt, so idle workers are no longer reported as stragglers.
- A loopback gateway whose stderr is a service log (systemd `JOURNAL_STREAM` / `INVOCATION_ID`, a launchd agent's
  `XPC_SERVICE_NAME`) names the token file and never prints the token.

## Tests

`tests/test_gateway_exit_supervision.py` (belt keeps 3, pending codes, pools stopped, token not printed under
systemd), `tests/test_gateway_cli_serve_host_controls.py::test_startup_failure_exits_with_uvicorn_code` (the
belt receives 3). Red when the belt exits 0 / when the service-log check is removed.

## Not in this item (raised)

- Root installer: `install.sh --server` should not leave a gateway running outside the systemd unit it
  installs (framework seat).
- Watchdog exit 75 on a hung event loop is non-zero: `Restart=on-failure` covers it (unchanged).
