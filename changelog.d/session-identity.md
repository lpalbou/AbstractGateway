### Added

- **A conversation can carry a temporary identity** (framework ADR 0052 §9: a regular agent's look and voice).
  `POST /runs/start` takes `identity` with a `session_id`, checked like an entity's identity (the design in the
  avatar catalog and drawable on the Watch, the voice offered, its model downloaded) and kept on the session
  (`session_identities.json` in the owner's data folder, never in an entity home). Every run of the session carries
  it with its reference `session:<sid>#<rev>`; a later turn inherits it, another identity is `rev + 1`. A session
  that names none has none (no Assistant identity exists on the gateway): no reference, no borrowed voice. The run
  TTS lanes speak with that voice when the request names none (`identity_ref`, `X-Identity-Ref`).
- **`abstractgateway entity identity get|set <name>` and `abstractgateway entity record <name>
  [--track-record|--reputation|--acquaintances]`**, through the running gateway's routes, in plain words ("No
  experience yet"; the completion-rate basis said in a sentence); `--json` prints the payload.

- **`GET /entities/{name}/identity[?rev=N]` says why the revision was written**: `identity.change` =
  `{cause, written_by, at}` from its biography row.
- **A visit's state (`GET /entities/{name}/visit/{run_id}/state`) carries the visit's `identity_ref`**, so an app
  rejoining a visit draws the right look before the first public update.

- **`GET /sessions/{session_id}/identity[?rev=N]`** resolves a conversation's reference to its look (the entity
  shape; `identity: null` when the session has none), and **`GET /runs/{run_id}` carries the run's `identity_ref`**
  when it has one (an entity visit's or a conversation's).

### Changed

- **The identity's voice refusals read for the identity page**: "Castor can't speak with zz: it isn't a kokoro voice
  this gateway offers. Choose one of: …" and "Castor's voice needs the kokoro model … which is not downloaded on this
  computer yet. Download it (…), then save the identity again." (they used to speak of the default voice).

- **An identity record or a track record that cannot be read names its file, not its absolute path**
  (`castor/identity.json: …`): `GET /entities/{name}/identity`, the voice and appearance routes, the entity TTS
  lanes, and the `record_unreadable` answer of the track-record, reputation and acquaintances routes. The gateway
  log keeps the full path.
- **A look change on an identity with no voice is refused with 400**, the identity checks' status for a missing
  voice (it was 409): a birth `appearance` on a gateway with no default voice, and `PUT /appearance` on a voiceless
  recorded default.
