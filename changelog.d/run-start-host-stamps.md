### Security

- **A client can no longer label a run as an entity's or mark its prompt as a repeat** (`routes/gateway.py`; review
  2026-11-09 finding 3). `POST /runs/start` and `/runs/schedule` drop a client's `_runtime.entity` (the entity doors
  mint and sign that stamp for the run they open; a client copy made AbstractRuntime's turn authors label the agent's
  reply as the entity's, replayed as "castor (entity): ...") and its `_meta.repeat_of` and `_meta.mention` (written
  only by the mention route after it validated them; a client-set `repeat_of` hid that prompt from a mention's quoted
  context and made clients fold it). `_stamp_run_origin` drops the two `_meta` keys as it already dropped `author` and
  `prompt_author`. With AbstractRuntime's matching change, an unsigned stamp never makes an entity turn either.
