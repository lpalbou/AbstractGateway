### Added

- **A visit's tool results rest whole in the entity's home, and three routes serve them** (`entities.py`,
  `entity_visits.py`, `routes/entity_replay.py`, `docs/entities.md`; root backlog 1145 steps 1 and 2, operator
  2026-11-08: no activity, human or entity, outside the runtime). The gateway's visit tool handler now passes the home's
  artifact store and the lane's context to the runtime's `execute_tool_elections`, so every tool result during a visit
  (the search output, a fetched page, the whole file behind a bounded `read_file` view, an image's bytes, a command's
  output) is kept whole, content-addressed, in `<home>/artifacts`; the result text ends with `[artifact: <id>]`, the
  ledger's `tool_calls` rows carry `artifact_id`, and the visit transcript's `tool_details` carry it too. New reads,
  from the home's own store and with no file path in any answer: `GET /api/gateway/entities/{name}/artifacts?run_id=
  &tool=&turn_id=&limit=&cursor=` (metadata rows, newest first, bounded), `…/artifacts/{id}/meta` (one row) and
  `…/artifacts/{id}` (the bytes with their stored content type; an image is served as an image); 404 for an unknown id.
  Needs the AbstractRuntime that carries `keep_tool_result_artifact` (its commit d217d93; the paired release).
