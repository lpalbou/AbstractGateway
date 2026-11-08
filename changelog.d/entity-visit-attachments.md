### Added

- **Files attached to an entity visit turn, as data** (`routes/entities.py`, `entity_visits.py`, `docs/entities.md`;
  operator 2026-11-08: images already work with every other client, so they must work for entities too).
  `POST /api/gateway/entities/{name}/visit/{run_id}/turn` takes `attachments: [{"path": "shared/x.png"}]` beside `text`
  and `speaker`; `path` is workspace-relative, the string `POST .../workspace/file` returned when the file was placed
  (`mounts/<name>/…` allowed). Each path is resolved through the entity's own workspace rules and recorded on the turn
  as `{path, bytes, sha256, content_type}` (type from the bytes for PNG/JPEG/GIF/WEBP, else from the name). Refusals come
  before any state moves, each with a `code`: 404 `attachment_not_found`; 403 `attachment_outside_workspace` (one fixed
  sentence for a path outside the workspace or an unknown or refused mount — the mount whitelist is never listed to a
  visit client); 400 `attachment_is_directory` / `attachment_path_required` / `attachment_unreadable` (an embedded NUL,
  a name too long, a symlink loop — never a 500); 413 `attachment_too_large` (the 20 MB per-file workspace cap, or a
  turn whose files total more than 64 MB, the total in the detail); at most 64 files per turn. The records ride the
  visit run's resume payload as structured data — nothing is parsed from the text — and
  `GET .../visit/{run_id}/transcript` serves them on the user turn as `attachments`. Meets pass none.
  `GET .../visit/{run_id}/ledger` serves media paths workspace-relative (a `read_file` result's `output.media`, an
  `llm_call` payload's `media`); the home store keeps the record as written.

### Fixed

- **An image attached in a visit reaches the entity's model as an image** (`entities.py`). The door's LLM handler
  dropped the payload's `media`, so a vision-capable mind never saw a screenshot the visitor attached: it received a
  path, and `read_file` on it returned the runtime's binary refusal. The handler now forwards `media` (absolute file
  paths) to `generate`, the way every other client's attachments travel; a vision error stays loud. The tool handler
  serves a `read_file` result that shows an image as the dict abstractagent's sight lane reads,
  `{"rendered": <the result text>, "media": [<absolute path>]}` (runtime's `tool_result_media`), so the image rides the
  entity's next model call; other tool results stay plain strings, and the transcript's `tool_details` serve the
  rendered text. Needs abstractruntime with `tool_result_media` (the paired runtime change).
