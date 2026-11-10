### Added

- **`POST /entities/{name}/summon` takes `mentions`**, with the `POST /runs/start` semantics: the composer's chips
  (never parsed from the text), resolved like the mention door and written host-only as `_runtime.mentions
  {entities, line}` (a client copy is dropped). The bundle host adds the line after the history, right before the
  person's words: the summoned entity answers only what is addressed to it, or says nothing with an empty reply.
  Naming the summoned entity itself mentions nobody else; an unknown name is 404 `entity_not_found`. The request
  model used to drop the key silently.
