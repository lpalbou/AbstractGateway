### Added

- **`communities_changed` on the replay stream** (COMMUNITIES-DESIGN 2026-11-10 §3): when a new version of his
  topics passes through the stream (memory's community records), the gateway records ONE host marker per version,
  payload `{kind: "communities_changed", version_seq}`, anchored where the version's records stand; every reader
  and the live tail see it, so the Entity app re-reads `/communities` instead of polling. Community records keep
  memory's replay projection: their binding draws the revision chain only, never an edge per member.
