### Fixed

- **Update works on Windows from the tray, the web console, the terminal console and `abstractgateway
  update`** (`self_update.py`). Check for Updates answered "AbstractFramework X is available, but not from
  here" and sent the person to a PowerShell line, because Windows keeps a running program's files locked and
  the installer must stop the gateway first. Update now hands off: the checked `scripts/install.ps1` (from the
  same commit as the release manifest, its sha256 shown in the confirmation and required to start) and a small
  runner are written to `<data dir>\update\`, and a short-lived launcher opens the runner in its own
  PowerShell window and exits, so the installer runs outside the gateway's process tree (the installer ends
  that tree). The installer stops the gateway, updates everything and starts it again with start at login as
  it was (`-NoOpen -NoModifyPath -DataDir <data dir>`). `windows-update.json` records the hand-off and the
  runner writes the installer's exit code next to it, so the gateway that comes back reports the outcome: the
  job is `completed` (with what moved), `failed` (the exit code and the log folder, or no report within 45
  minutes while the installer no longer holds its lock), or `running` while the installer finishes; the tray
  follows it and says once how it went. A failure is reported by the gateway that started the update too (the
  installer can fail before it stops the gateway), and the next check offers the update again. The runner
  checks the confirmed sha256 of `install.ps1` again right before it runs it, ignores Ctrl+C, always records
  the result, and starts the gateway again (`serve`) if a failed installer had stopped it; the update folder is
  restricted to the account. A second update is refused while one runs, and a window that could not be opened
  leaves no record. The update window stays open when the installer fails, with the path of its log. While
  the installer runs the job is `running` with `job.handoff` set (every client already follows a running job);
  the new job state is `completed`.
