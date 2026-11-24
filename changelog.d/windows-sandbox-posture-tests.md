### Added

- **The command-sandbox state names the drives Windows cannot protect** (`command_sandbox.py`). On a
  Windows host, the `sentence` of `command_sandbox` (shown as the console's tooltip and by every client) ends
  with "Not protected: drive D: has no permissions (exFAT): commands can read and change it" for each drive
  without permissions or network share AbstractCore reports (through the runtime facade's
  `host_unprotected_volumes`, AbstractRuntime with it and AbstractCore 2.39.0; nothing with older ones).

### Changed

- **The command-sandbox tests know a Windows host that sandboxes both postures**
  (`tests/test_r12w2_command_sandbox.py`, `tests/test_r12w2_e2e_command_sandbox.py`). The state test reads
  `Commands sandboxed: Windows restricted token` (state `sandboxed`, tool card "Sandboxed to this run's
  workspaces") when AbstractCore reports a Windows sandbox for both postures. The end-to-end test has a scenario
  of its own for "Deny everything, allow listed workspaces" and for "Allow everything" with read-only by default
  on Windows (one run through `POST /runs/start` and the real tool path: refused rows, `.ssh` and an unlisted
  folder unwritable, the reopened child, a read-only row and the run's own folder with its `TEMP` as positive
  controls, unlisted user data hidden under the allow-list and readable otherwise). It runs when AbstractCore's
  Windows sandbox enforces those shapes (`host_sandbox_kind("allowed_only")`, the release after abstractcore
  2.38.0) and skips with that reason before.
