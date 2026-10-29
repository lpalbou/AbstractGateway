### Fixed

- **A gateway-side generation uses the model the runtime already holds** (R2 of the 2026-10-29 residency audit). The
  sandbox, a run summary and entity chat each built a client of their own per request, and each loaded its own copy
  of the weights: on a Mac with MLX, one warm-up, one chat and four concurrent sandbox calls left seven instances of
  the model in the model host child. They now borrow the principal's runtime pool for the call (`_provider` and
  `_model` per call, with only a `base_url`/`api_key` that differ from the pool's own), so the child holds one
  instance however many requests run at once. A host without a runtime pool builds its own client as before. In
  `--model-host process` mode the gateway's embeddings client is built in the child too, so no model loads in the
  gateway process. The gateway and runtime add no lock: concurrency on one weights object stays with Core.
  `tests/test_model_host_real_model_e2e.py` (opt-in, `ABSTRACT_E2E_MODEL_HOST_REAL_MODEL=<provider>:<model>`) boots
  a real gateway on a cached model, runs a chat and N concurrent requests, kills the child, chats again, and asserts
  one build of the model per child and one weights holder in `/api/gateway/models/loaded` at each phase.
