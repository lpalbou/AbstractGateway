### Fixed

- **`abstractgateway service uninstall` waits for the whole gateway process tree** (backlog 0934). It captures the
  gateway's processes before launchd or systemd stops the serve process: the recorded serve and app pids, this
  data folder's app watcher, and every descendant, including the children started in their own session (entity
  loops, model downloads, managed processes, apps, the tray). It waits up to 20 s for them to exit, then sends
  SIGTERM, then SIGKILL, and says what it stopped. It fails, naming the process, when one survives. On Linux
  XDG autostart, where no service manager stops the gateway, it stops the gateway itself. `--dry-run` shows the
  step, and `service disable` still stops nothing.
- **mflux and MLX images & video are never offered off a Mac** (operator QA 2026-10-28, item d). The capability
  editor's provider list (`GET /api/gateway/vision/provider_models?providers_only=true`) offered both on the Linux
  server. The prune applied only to the `abstractvision.local` answer, and the providers-only answer comes from the
  runtime's local catalog (`abstractruntime.discovery`); its `details` named them too. Every local answer is pruned
  now, `details` and `image_providers` included. An answer from a remote Core (`base_url`) is kept, since that host
  may be a Mac. On Linux the processes are read from procfs (whole command lines); elsewhere `ps -ww`, never cut to the terminal width.
  may be a Mac.
