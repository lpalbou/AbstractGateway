### Fixed

- **A mention of a name no entity can have answers 404, never 500** (`routes/session_mentions.py`; review 2026-11-09
  finding 4). `POST /sessions/{sid}/mentions` with `{"entity": "@Castor"}`, a reserved word (`auth`, `templates`, ...)
  or any name that does not normalize answered 500 with the internal error text (`ValueError: entity name '@Castor'
  does not normalize ...`). The name is now checked first and answers the same 404 `entity_not_found` sentence as a
  missing or hidden entity (`There is no entity called '@Castor'.`); a malformed body stays 422.
