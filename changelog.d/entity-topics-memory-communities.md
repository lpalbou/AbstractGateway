### Changed

- **His topics are memory's communities, formed while he sleeps** (COMMUNITIES-DESIGN 2026-11-10; AbstractMemory
  `community_index.communities`, AbstractRuntime `memory_facade.communities`). `GET /entities/{name}/communities`
  keeps its URL and serves memory's payload version 2 unchanged: `{version: 2, version_seq, first_sleep_pending,
  algorithm, communities: [{id, key, title, summary, pending, verdict, size, kinds, members: [{id, via}],
  provisional_ids, cohesion, layout: {xy, radius, seats}, centroid_stale, facts}], links: [{a, b, weight}]}`. Titles
  and summaries are authored (or honestly `pending`), never keyword labels. Nothing is computed on GET: the runtime's
  request-time Louvain (`abstractruntime.identity.communities`, retired) is no longer called; a home with no version
  yet answers `first_sleep_pending: true` with no communities, never a stand-in partition. Cached per home and journal
  seq, so a memory placed since the version shows in `provisional_ids` at the next read.
- **The `topics` tool reaches visits**: the door's tool executor wires AbstractRuntime's `HomeMemoryReader.topics`
  beside `search_memory`/`read_memory`/`recent_memories`.
- **`sleep.after_fragmented_share` in the cognition profile** (AbstractRuntime 9350986): `GET/PUT
  /entities/{name}/cognition-profile` serve and accept the share of his memories not yet in a topic past which an idle
  entity sleeps (default 0.15, `0 < share <= 1`, the runtime's sentence for a value outside it; null resets it).
