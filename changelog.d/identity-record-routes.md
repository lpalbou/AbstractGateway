### Added

- **Track record, reputation and acquaintances, read-only** (`entity_record.py`,
  `routes/session_record.py`; the framework's ADR 0052). `GET /api/gateway/entities/{name}/track-record`
  (`?since=`), `/reputation` and `/acquaintances` read the entity's own home; the same three under
  `/api/gateway/sessions/{session_id}/` read a regular agent's session in the caller's runtime plane
  (another account's session is 404 `session_not_found`). The answer is AbstractRuntime's, unchanged:
  `basis: "completion-rate only"` and `not_counted` included. A record the runtime cannot read is a
  500 `record_unreadable`, never an empty record. Needs AbstractRuntime 0.18.0. See
  [docs/api.md](docs/api.md#track-record-reputation-and-acquaintances).
