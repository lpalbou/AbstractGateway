### Added

- **Session kinds `automation` and `process`** (`session_kinds.py`, `GET /runs`, `POST /runs/start`, `GET /host/runs`;
  backlog 1166, operator 2026-11-08: "conversations, docs, notes, automations, processes; a probe is a process; a
  visit is a conversation with an entity"). A session's purpose is now one of `conversation` (the default), `docs`,
  `notes`, `automation` (an occurrence of an automation a person created: the controller's and its occurrences'
  sessions, by their run attribution) or `process` (work the framework runs for its own treatments: AbstractNote's
  `note-process`, `note-reflect` and `note-consolidate` occurrences by exact flow id; the phone and watch voice probes
  and model-host verifications when their client starts them with `kind: "process"`). Note's captures and drains stay
  `notes`; a discussion stays a conversation; there is no `visit` kind. `POST /runs/start` takes the new kinds (with a
  `session_id`) and marks the session before the run starts. Turn listings take `kind=` as a comma list
  (`kind=conversation,automation`) beside `all`; the default stays conversations, so automation occurrences leave the
  default list; a listing that asks for the automation attribution (`session_kind=automation,occurrence`, what the
  Observer and AbstractCode send) keeps listing those sessions whatever `kind` says. Every row carries `kind` and
  `session_kind`. The first listing after the upgrade runs the one-time migrations `process_sessions_v1` and
  `automation_sessions_v1` (recorded in `session_kinds.json` like `docs_qa_bundle_v1`); later occurrences are
  classified at read time, never written to the file. `GET /host/runs` lists automation sessions by default and takes
  `kind=process` / `kind=automation`; processes stay out of its default like notes. The client halves (the Assistant's
  probes sending `process`, the Code and Observer kind chips) are separate items.
