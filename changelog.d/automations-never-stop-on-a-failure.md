### Changed

- **A failure never turns an automation off.** A run whose workflow is not installed (or not allowed) fails once with
  its reason and the automation stays active, with all its controls, and tries again at its next scheduled time;
  `paused_reason` only ever says why the gateway paused an automation (an administrator made its workflow
  unavailable), never a failure.
- **An automation that stopped on an error comes back by itself** (`runner.py`). When the runner starts, and after any
  tick that leaves it stopped (its tick failed, or an effect failed without raising), the automation is brought back:
  the run it was on is shown with its real outcome or as failed, a run still going is followed to its end, a time
  missed while it was down runs once when it is back, then its normal times; one that stops at every attempt tries
  once per scheduled time, each attempt shown as a failed run. One a person stopped, paused or archived stays so. A
  stopped automation (archived or not) never shows a run as running. Requires the matching AbstractRuntime.
