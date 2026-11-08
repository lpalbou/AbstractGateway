### Fixed

- **A conversation list reads the run store once per refresh** (`GET /api/gateway/runs`). With the run index in
  `verify` mode (the `serve` default), every read of the run store goes through the whole run folder (one
  directory listing and one `stat` per run file), whatever the page size. A `root_only=true` page, and the
  Observer's `kind=conversation&session_kind=automation,occurrence` page, read the store again with a wider
  limit each time their filters (internal workflows, draft tests, archived sessions, notes) left the page
  short: two reads on a 14,000-run store, three or more on stores with many hidden runs, each slower when
  other work holds the interpreter (the Observer's 30 s refresh deadline, "Loading conversations…" in
  Code). The listing now remembers how deep the same listing had to read last time and starts there: one
  read per refresh after the first, with the same rows in the same order. On a copy of a 14,400-run store,
  a warm `root_only=true&limit=200` page went from two reads (0.31-0.64 s) to one (0.15-0.29 s). The remaining
  cost is the `verify` scan itself; `serve --run-index trusted` answers the same page in about 60 ms.
