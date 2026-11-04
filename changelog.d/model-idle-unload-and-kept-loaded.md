### Added

- **Unused models unload after 60 minutes; Keep loaded survives restarts** (root backlog 1115). A loaded
  model that no request has used for the idle-unload time is unloaded from memory by AbstractCore; the
  next request loads it again. The time is `serve --model-idle-unload MINUTES` (for one run) or the saved
  setting `models.idle_unload_minutes` (default 60, `0` never; Models page **Unload unused models after
  (minutes)**, `abstractgateway config set models.idle_unload_minutes MINUTES`). A model kept loaded never
  unloads, and the gateway now records the lock and sets it again after a gateway or model-host restart
  (an unlock or a forced unload ends it). The Models page and the terminal console say, per loaded model,
  "Unused for 12 min, unloads in 48 min", "Kept loaded: never unloads", "In use now" or "Stays loaded:
  unloading unused models is off". Needs AbstractRuntime 0.18 (`set_model_idle_unload`,
  `set_model_residency_locks`); with an older runtime the gateway logs an error naming it at start and
  after every change, and nothing is unloaded.
