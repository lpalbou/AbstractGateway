### Fixed

- **An unreadable AbstractCore settings file is a 503 that names it** (AbstractCore 8b970550 `ConfigParseError`: the
  file is not JSON, or not an object; Core never runs on defaults then). The routes that read Core settings answered
  500 "Internal error"; they answer 503 `core_config_unreadable` with the file's path and the parse error, the file is
  left exactly as it is, and the rest of the gateway keeps serving.
