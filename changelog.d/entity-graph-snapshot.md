### Added

- **`GET /entities/{name}/graph`: his memory graph in one read** (COMMUNITIES-DESIGN 2026-11-10, slice 2). The
  Entity app drew the graph by folding the whole replay through the live door. This route serves `{version,
  journal_seq, communities_version, communities, nodes, edges}`: every open formed record but the community records
  (the latest display block of its binding, resolved for the operator exactly as the replay serves it), its
  community `key`, `via` and seat from memory's communities read (the payload itself rides along unchanged), and
  the formation edges between nodes. Folded once per process and home, then only the replay's tail since
  `journal_seq` (a new memory, a closure, the night's new version); the app tails the live stream from there.
  Measured on a scratch copy of castor (895 nodes, 2947 edges, 14 topics, 1.9 MB): the first read 2.1-2.4 s (once
  per process and home), then 5 ms; after a new memory formed, 62 ms (the tail only).
