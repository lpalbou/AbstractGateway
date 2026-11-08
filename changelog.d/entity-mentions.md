### Added

- **`@entity` from any chat window: `POST /sessions/{session_id}/mentions`** (`entity_mentions.py`,
  `routes/session_mentions.py`, docs/entities.md "Mentioning an entity from any chat"). Body `{entity, text,
  mention_index}`; 200 `{run_id, turn_id, author, session_id, mention_index, reply, visit: {run_id, turn_id, visit_id,
  entity_id, reused}}`. The mention runs through the VISIT DOOR as the signed-in person: the entity's live visit is
  reused when it is bound to this session, else a visit is opened bound to it (channel `mention`, closed by the
  reaper after the seat's 5 idle minutes, reflection included); `text` is a visit turn, so the entity's HOME holds
  the episode (ledger, memory formation, artifacts). The reply is recorded as a turn of the session (a completed root
  run, workflow `entity-mention`, `_meta.author` the entity, `_meta.prompt_author` the person, `_meta.mention` the
  home's visit run/turn ids). One session's mentions are serialized. `repeat_of` (optional, the person's own turn of
  this conversation carrying the same words) marks the mention turn `_meta.repeat_of` so clients show the words once
  (the prompt is kept: replay stays whole pairs); 422 `repeat_of_unknown` otherwise. The mention turn's `created_at`
  is when the words arrived, `updated_at` when the reply came. In a VISIT lane (the session is another entity's live
  visit) the reply is mirrored into that visit as a turn spoken by the mentioned entity (door join:
  `EntityVisitHost.join_participant` re-signs the stamp with the new participant, then `turn` with that speaker and
  the words `<name> (entity): <reply>`); the visited entity answers it, its home ledger keeps it; listed under
  `mirrors`. Only the VISITED entity's own visit is mirrored into (a visit the mention door opened never is: two
  entities mentioned in one chat do not mirror into each other). The mentioned entity READS the conversation: its
  visit turn is the session's newest whole turns quoted with their authors (`Laurent: ...`, `Agent (<name>): ...`,
  `<Name> (entity): ...`, from the turn rows' `author` fields), at most 12 turns / 8,000 tokens whichever first,
  then the person's words; the answer's `context` states the bound. Visit runs carry `_meta.author` (the entity's
  display name, identity ref, look). The 404 sentence names the entity only (no server folder path). Errors `{detail, code}`: 404 `entity_not_found` for an unknown or a hidden entity alike (members
  mention only the entities they created; same sentence, same code); 409 `in_another_conversation` naming
  the conversation holding the visit (`session_id`); the visit door's own 409 codes; 503 `entity_cannot_answer`
  (mind unresolvable: the visit door's substrate refusal now carries code `mind_unresolved`; or no reply).
- **Who speaks, on every turn surface: `author` / `prompt_author`** (`service.run_summary`, `events_stream`,
  `session_history_bloc.py`, AbstractRuntime `core.turn_author`). `{kind: entity|agent|person, name, entity_id,
  identity_ref, avatar}` (every key present) on `GET /runs/{id}`, history bloc rows (and each bundle's `run`), the
  replay view's user/assistant messages, the `run.started`/`run.finished` pointer events (`author`) and every live
  reply frame on `/runs/{id}/ledger/stream` (`llm.delta`/`llm.delta_end`, snapshot and synthetic ends: the root
  turn's `author`, resolved once per root by the delta sink, so it rides split-mode files too). `/runs/start`
  stamps the person under the account's display name and drops a client-sent author; a summon stamps the entity as
  its answer's author. The history replayed to the next agent run renders an entity's reply `<name> (entity): ...`.
