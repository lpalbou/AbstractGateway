### Fixed

- **A started run keeps its workflow snapshot** (`hosts/bundle_host.py`, backlog 1009). The update that adds the
  snapshot reference and the session id right after a run starts now holds the run's mutation lock, so it no
  longer races the runner's first step (the reference was lost, or a finished run went back to its first state).
  The start waits at most 0.5 s for it.
- **Push notifications follow a forwarder setting saved mid-pass** (`push/dispatcher.py`, backlog 1009). The
  dispatcher takes its change tokens before reading the setting and the registered devices, so a save that lands
  between the read and the wait is no longer missed.
- **A gateway-level test that `/v1` reuses the model the runtime pool holds** (`tests/test_v1_reuses_resident_model.py`,
  backlog 1008), so a second copy of a loaded model cannot come back unnoticed.
