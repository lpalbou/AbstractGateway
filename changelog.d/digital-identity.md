### Added

- **Every entity has a digital identity: a look and a voice, one record in its home** (framework ADR 0052, backlog
  1119; AbstractRuntime's `identity.json`). `GET /entities/{name}/identity[?rev=N]` serves the record (the design with
  its Watch code, the colour, the voice), its reference (`entity:castor#3`), the fingerprint and the age;
  `PUT /entities/{name}/identity` (an admin or the creator) sets the whole record, refused in this order for every
  role: the home is closed for maintenance (409), the design is not in the avatar catalog this gateway loaded or can't
  be drawn on the Watch (400), the voice is not offered here (400), its model is not downloaded (409 with the download
  action). An identity is never cleared. An entity born now gets its identity at birth (`identity` on
  `POST /entities`, else the recorded default; a birth `appearance` becomes its look), and every existing home gets
  the recorded default once when the gateway starts (homes closed for maintenance are skipped and logged), else at its
  first `GET /identity`. The entity speaks with the record's voice and never another: its TTS lanes answer
  `voice_source: identity` with `identity_ref` and `X-Identity-Ref`, and a record with no voice refuses with "Castor has
  no voice yet: choose one in its Identity settings or set the Gateway default voice". The reference rides each visit
  (beside the signed stamp), meet leg, relay and status row, and public update row; the list rows carry `identity`.
  New dependency `abstractavatar>=0.1.1`; needs AbstractRuntime 0.18.0.

### Changed

- **On a home with an identity record, `voice` and `appearance` read and write through the record**: `GET` serves the
  record (`source: identity`; the appearance's shape is the one its design draws), `PUT` changes it (`rev + 1`, the
  identity's checks for every role), `clear` is refused (409). A home without a record keeps the old files and their
  behaviour. The list row's `appearance` is the record's look.
- **Visits, meet legs and summons record who started them** (`_meta.origin`, kind `person`, the actor in the same
  `person:<user>` form as the participants), so they count in the entity's track record; a visit is told an identity
  change made since the previous summon (`_visit.identity_since`).
- **The identity card shows the home's biography markers** (`phase_changed`, `identity_changed`) with their time
  instead of a blank "state" moment.
