### Added

- **This Mac stays awake while remote access is on** (`keep_awake.py`). A Mac set to sleep when idle used
  to drop the phone and the watch as soon as nobody touched it. While the gateway runs with the `internet`
  network exposure, or Tailscale Serve or Funnel forwards to its port, it now holds the macOS
  `PreventUserIdleSystemSleep` assertion "AbstractGateway: remote access" (what `caffeinate -i` holds).
  - Only idle sleep is prevented: choosing Sleep in the Apple menu, closing the lid without an external
    display, or pressing the power button still sleeps the Mac, and the display still turns off. It applies
    on battery and on the charger.
  - The gateway process holds it itself (IOKit), so it ends when the gateway stops or crashes. Nothing is
    installed and no power setting is changed.
  - On by default. Turn it off with the **Keep this Mac awake while remote access is on** switch on the
    console's Resources page (Gateway card → **Manage gateway** → **Stay awake**), with
    `abstractgateway config set keep_awake off`,
    or for one run with `serve --keep-awake off`. A change applies at once; Tailscale Serve or Funnel turned
    on or off is noticed within a minute.
  - The Resources page's Gateway card reads **Staying awake for remote access** or **Not keeping awake**, with
    the reason. The tray's Connect (Network) menu shows the same line.
  - `GET /api/gateway/host/keep-awake` (any signed-in user, contract `gateway_keep_awake_v1`) serves the
    state; `GET /api/gateway/network` carries it as `keep_awake`; `keep_awake` is a runtime setting
    (`POST /api/gateway/admin/runtime-config`, admin), whose response carries the new state as
    `keep_awake_status`.
  - Linux and Windows: not available yet; the state says so and `serve` logs one line.
