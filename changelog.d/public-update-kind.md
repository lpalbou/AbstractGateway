### Added
- **Entity visit state carries the type of each public update** (`entity_session_tools.py`).
  `public_updates` rows have `update_kind`, `"finding"` or `"progress"`, as typed by the actor
  and as the replay view reports it. See [docs/entities.md](docs/entities.md).
