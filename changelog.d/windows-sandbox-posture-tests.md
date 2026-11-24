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
