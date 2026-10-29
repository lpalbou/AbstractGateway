### Added

- **The Models row says what context a model reserved and why, and an admin sets it** (AbstractCore ADR 0010 point 8,
  operator ruling 2026-10-29). The Context cell reads "65,536 (model advertises 262,144; limited by memory)" or
  "(set for this model)"; "Set context" stores a per-model context (`PUT /api/gateway/models/context`, AbstractCore's
  `model_contexts`): loaded as is at the model's next load, refused with the readable sentence when it does not fit,
  never shrunk; empty returns to the fit to memory. Rows carry `advertised_context_length` and `context_source`.
  Needs AbstractCore with `abstractcore.config.model_contexts`.
