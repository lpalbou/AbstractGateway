### Added

- **One memory says which of his topics it is in.** The record read (`GET .../records/{graph_id}/verbatim`) and
  the card's record briefs (interests, lessons, key moments) carry `community`: `{key, title, pending,
  provisional, via}` (AbstractMemory `community_of` through the runtime facade for the record read; the same
  communities read `/communities` serves for the card), or `null` when no version holds the memory, so a hover
  card or the Detail tab can say "in topic …". A diary record's read is unchanged.
