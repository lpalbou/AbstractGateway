### Fixed

- **Quit from the tray stops the gateway at once and removes its icon** (`host_control.py`, `tray/app.py`,
  `tray/events.py`). Quit (and Restart) from the tray or the console set uvicorn's `should_exit` without
  beginning the bounded stop of `shutdown_drain.py`, so the tray's own `/me/events/stream` held every
  requested exit for the whole 10 s drain ("Cancel 1 running task(s), timeout graceful shutdown exceeded").
  A requested exit now ends live streams the moment it is asked for, as a stop signal does. On Windows the
  icon then stayed in the notification area as a ghost: the tray's `stop()` stopped its event follower
  first, whose `resp.close()` waited for the lock the blocked stream read held (Windows does not wake that
  read on `shutdown()`), so `Icon.stop()` was never reached and the gateway terminated the helper after 3 s
  with its icon still registered. The tray now removes its icon first and the follower's `stop()` never
  blocks. Measured on Windows 10 (a `serve`-shaped stub gateway with the real tray): Quit to icon removed
  went from 13.2 s, with the helper killed and its icon left behind, to 0.3 s with the icon deleted and the
  helper exiting 0.
