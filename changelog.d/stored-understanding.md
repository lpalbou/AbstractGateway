### Added

- **What a memory stores about its own words, on every surface the Entity app reads** (`entity_understanding.py`,
  `routes/entity_replay.py`, `routes/entities.py`, `docs/entities.md`). AbstractMemory now rewrites a record's words
  in place and keeps every earlier reading as a version; the gateway serves its fields under memory's own names, every
  key present and `null` when the store holds none: `digest`, `digest_method`, `digest_version`, `version_count`,
  `authored_by` `{kind, id}`, `reader` `{provider, model, thinking}`, `understood_at`, `cause`; world-model cards
  `target`, `target_type`, `card_revision`, `source_count`; dreams `narrated`, `narration_version`; a superseded record
  `replaced_by`. `GET /api/gateway/entities/{name}/records/{graph_id}/verbatim` carries the current reading and
  `versions` (every reading, oldest first). Replay `snapshot` envelopes carry `display.recalled`, one block per recalled
  record, describing the reading that entered that context (the payload is unchanged; with `enrich=false` nothing is
  added). `GET .../card` interest, lesson and key-moment briefs carry the current reading. Diary records gain nothing:
  their words still leave only through the diary door. Requires the AbstractMemory release with
  `MemorySystem.record_understanding` and `versions` (memory 0059).

### Fixed

- **A session summary formed without its verbatim opens instead of answering 404**
  (`routes/entity_replay.py`). `GET .../records/{graph_id}/verbatim` on a `summary` record with no `payload_ref` answers
  200 with its digest as `text`, `born_digest: false` and `summarizes` (the graph ids of the records it covers). Any
  other record without a verbatim is still a 404.
