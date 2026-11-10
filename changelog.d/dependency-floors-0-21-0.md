### Changed

- **Dependency floors for this release** (`pyproject.toml`): AbstractRuntime 0.19.0 (the Stop fence, the one
  iteration default, registered skill roots, the entity life cycle), AbstractMemory 0.5.0 (the task queue every
  entity home opens with, the fragmented-memory counts the idle sleep reads), AbstractCore 2.35.0 (the runtime's own
  floor), abstractavatar 0.2.0 (retired designs and resolved styles), abstractagent 0.6.1 and abstractskill 0.3.11.
  The `apple` and `gpu` profiles and the `embeddings` extra follow. The abstractvoice floor stays 0.20.0.

### Fixed

- **A gateway on an AbstractRuntime older than 0.19.0 says which version it needs** (`runtime_config.py`,
  `live_deltas.py`, `cli.py`). The iteration default is read from the runtime when it is first needed, so the
  app still loads on an older runtime, and the startup check refuses it by name: "this gateway needs
  abstractruntime>=0.19.0 ... Upgrade it: pip install -U 'abstractruntime>=0.19.0'", listing what is missing (the
  iteration default, the Stop fence, registered skill roots). It used to stop with an ImportError at import.
