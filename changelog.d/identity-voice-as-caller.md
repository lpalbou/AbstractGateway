### Fixed

- **Saving an identity with a voice the picker shows works again** (`entity_settings_access.py`,
  `entity_identity.py`). "Castor can't speak with 250b665e…: this gateway offers no qwen3-tts voice
  right now" while the signed-in `GET /voice/voices?provider=qwen3-tts` listed 93 voices: the check
  called the voice route function directly, and the `language` parameter it did not pass arrived as
  FastAPI's `Query(None)` object, which filtered every voice out. The voice and model checks
  (identity, `PUT /entities/{name}/voice`, the mind's model, the identity at birth) now ask the route
  as the caller (their principal and plane, every parameter at its declared default), so the voices
  the caller's picker lists are the voices a save accepts.
- **A conversation's identity accepts a voice again** (`POST /runs/start` with `identity`). Its voice
  check ran on the event loop, where it could not run, and refused every voice ("asyncio.run() cannot
  be called from a running event loop"); it now runs off the loop.

### Changed

- **Whose voices an entity may speak with, said in the refusal.** An entity lives and speaks in its
  creator's plane, so it may speak with the gateway's shared voices (built-in, made or shared by an
  administrator) and its creator's own voices; another person's own voice, or an unknown id, is
  refused with "it isn't a qwen3-tts voice Castor can use. An entity speaks with this gateway's shared
  voices and its creator's own voices; another person's own voice stays theirs until an administrator
  shares it with everyone." (docs/entities.md, "Whose voices an entity may speak with").
