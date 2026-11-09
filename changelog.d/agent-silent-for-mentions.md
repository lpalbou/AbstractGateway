### Added

- **The conversation's agent stays out of what is addressed to entities** (operator agreed 2026-11-09).
  `POST /runs/start` takes `mentions`: the entities the message mentions (the composer's chips, the
  same names the client sends to `POST /sessions/{id}/mentions`; never parsed from the text). Each is
  resolved like the mention door does (a hidden or missing one is 404 `entity_not_found`), and the
  agent reads one line after the conversation's history: "The mentioned entities (Castor) answer for
  themselves; answer only the part addressed to you, or say nothing with an empty reply." Whether the
  message also addresses it is the agent's own judgment. The line rides `_runtime.mentions`
  (`{entities: [{entity_id, name}], line}`), written by the gateway only (a client copy is dropped),
  and is never replayed into a later turn. An empty reply shows no answer: the turn keeps the person's
  words and the entities' answers follow it.
- **Needs** the agent to take an empty reply as its answer when `_runtime.mentions` is set: the
  ReAct loop (AbstractAgent) still asks again after an empty reply ("Your previous response was
  empty"), and the clients to send `mentions` with the turn.
