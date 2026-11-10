### Added

- **`cut`: the words were cut when they were saved** (FINDINGS 2026-11-09 §(4): "[#TRUNCATION]" inside end-user
  hover cards). Every record block the Entity app reads now says whether its words were cut by a retired
  writer when it saved them: the replay stream's record blocks (`display.cut`) and recall blocks
  (`display.recalled[].cut`), the record read (`GET …/records/{graph_id}/verbatim`) and the card's record
  briefs. The flag comes from the writer of the words shown, never from their text (no marker is looked
  for): the flow brain's `mechanical-flow-v1` episodes, and a session reflection still in the words it was
  formed with under no recorded author (until ADR 0054, AbstractRuntime 2a1c698, that writer cut its digest
  at 280 characters and stamped nobody). A rewrite in his sleep clears it. The app can then show the title
  and "Older record: its summary was cut when saved; he rewrites it in his sleep."
- Known gap: on a machine that ran AbstractRuntime between 2a1c698 and the authored-at-formation release,
  reflections formed then were whole but are also unstamped, so they read `cut: true` until rewritten.
- The identity card's statement blocks (`GET /entities/{name}/card` `identity`: values, purposes, traits) carry `cut`
  too, from the writer stamp of each statement's own self record (matched by kind and title), never from the words.
