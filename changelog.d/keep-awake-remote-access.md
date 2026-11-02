### Added

- **While the gateway runs, the Mac does not idle-sleep** (`keep_awake.py`), so phones and watches keep reaching
  it. `abstractgateway serve` holds the macOS `PreventUserIdleSystemSleep` assertion "AbstractGateway" until it
  stops; closing the lid or choosing Sleep still sleeps the Mac, and the display still turns off. Other systems:
  unchanged.
