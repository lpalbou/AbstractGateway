### Changed

- **AbstractNote's own work is `notes`, never a conversation or a user automation**
  (`session_kinds.py`, `routes/automations.py`). Conversation lists showed "Conversation note-wat…"
  sessions and the turn listing `GET /runs?root_only=true` was mostly `abstractnote@…:note-drain`
  occurrences; every Automations list showed "Process my notes", "Reflect on my notes" and
  "Consolidate my notes". A session whose turn root runs an AbstractNote workflow (the
  `abstractnote` bundle id, matched exactly) is now `kind: "notes"`, like the Docs assistant's
  `docs`: `POST /runs/start` marks it (a capture from Note, its phone or watch), a one-time
  migration (`abstractnote_bundle_v1`) marks the existing ones, and the sessions Note's
  automations open are `notes` by their workflow. Turn listings and `GET /sessions/search` leave
  them out by default and list them with `kind=notes`. `GET /automations` takes `kind=user`
  (the default), `notes` or `all`, every row carries `kind`, and the paused and archived counts
  count the listed kind. A purpose, never the creating client. AbstractNote reads its automations
  by id, so its Processing page is unchanged. A docs-qa run named by a namespaced `flow_id`
  (without `bundle_id`) is now marked `docs` too.
