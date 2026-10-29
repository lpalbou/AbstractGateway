### Fixed

- Native model-host readiness and structured run calls work independently of
  optional HTTP listeners. Request-encoding failures are nonretryable and carry
  run/step correlation where available.
- Optional listener shutdown failures remain visible, and unchanged listener
  status is not repeatedly written to disk.

### Added

- Independent `direct_enabled` control for Core-owned HTTP serving with its own
  bind and authentication, sharing the execution owner's model resources.
  Existing Gateway `/v1` keys and the `enabled` setting retain their behavior.
- An AbstractGateway agent skill for authenticated runs, canonical replay,
  approvals and inference endpoint operation.
