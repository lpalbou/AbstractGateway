### Changed

- **Engines are installed by the gateway's administrator, once per computer** (operator ruling 2026-10-29). Every user
  shares them, so the console's Engines tab and the terminal console's Local providers have no "Allow engine
  installs" switch. An account that is not an admin reads "An administrator installs engines for this computer."
  The refused-install message and the network note name the CLI (`abstractgateway-config set allow_engine_install
  on`) for the gateway's operator.
