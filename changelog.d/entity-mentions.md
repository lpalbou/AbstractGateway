### Added

- **`@entity` from any chat window: `POST /sessions/{session_id}/mentions`** (`entity_mentions.py`,
  `routes/session_mentions.py`, docs/entities.md "Mentioning an entity from any chat"). Body `{entity, text,
  mention_index}`; 200 `{run_id, turn_id, author, session_id, mention_index, reply, visit: {run_id, turn_id, visit_id,
  entity_id, reused}}`. The mention runs through the VISIT DOOR as the signed-in person: the entity's live visit is
  reused when it is bound to this session, else a visit is opened bound to it (channel `mention`, closed by the
  reaper after the seat's 5 idle minutes, reflection included); `text` is a visit turn, so the entity's HOME holds
  the episode (ledger, memory formation, artifacts). The reply is recorded as a turn of the session (a completed root
  run, workflow `entity-mention`, `_meta.author` the entity, `_meta.prompt_author` the person, `_meta.mention` the
  home's visit run/turn ids). One session's mentions are serialized. Errors `{detail, code}`: 404 unknown or hidden
  entity (members mention only the entities they created, hidden = missing); 409 `in_another_conversation` naming
  the conversation holding the visit (`session_id`); the visit door's own 409 codes; 503 `entity_cannot_answer`
  (mind unresolvable: the visit door's substrate refusal now carries code `mind_unresolved`; or no reply).
- **Who speaks, on every turn surface: `author` / `prompt_author`** (`service.run_summary`, `events_stream`,
  `session_history_bloc.py`, AbstractRuntime `core.turn_author`). `{kind: entity|agent|person, name, entity_id,
  identity_ref, avatar}` (every key present) on `GET /runs/{id}`, history bloc rows (and each bundle's `run`), the
  replay view's user/assistant messages and the `run.started`/`run.finished` pointer events (`author`). `/runs/start`
  stamps the person under the account's display name and drops a client-sent author; a summon stamps the entity as
  its answer's author. The history replayed to the next agent run renders an entity's reply `<name> (entity): ...`.
