# Summoned entities

The gateway owns the lifecycle of **summoned entities** — persistent
identities (like "Castor") that live across sessions, users, and workplaces;
each summon is a re-adoption of the same self (earlier experiments called
them *incarnations*). An entity is not a chatbot configuration: it is a home
directory holding everything the entity is and has lived, plus the lifecycle
surface to create, inspect, verify, and summon it.

This page uses human words first, API names in parentheses.

## What an entity is

A summoned entity lives in two files at its **home**:

- **Its memory** (`memory.sqlite3`) — the involuntary record: everything that
  happens to it, its standing feelings about anything it has experienced
  (people, tools, ideas, places, even a time of day), and its always-on
  identity core planted from a spark document. Things happen to you and you
  are forced to remember; that is what forges who you are.
- **Its diary — "the book"** (`home.sqlite3`) — the voluntary record: what
  the entity *elects* to write. First-person, hash-chained, written only by
  the entity itself, and **never deletable — there is no delete surface
  anywhere in the code, on disk, in the CLI, or over HTTP.**

Next to those live the **attested seed** (`spark.yaml`, stored byte-verbatim
at creation — the spark is engrammed once and kept for life) and the
gateway's identity card (`manifest.json`: the entity id
`entity:<name>@<home-id>`, creation time, the spark hash, and reserved fields
for the future key/signature work).

**Copying the home directory moves the entity.** The `home_id` in the
manifest names the birth home and is kept on copy; re-homing is deferred to
the keys work.

## Lifecycle surface

CLI (there is deliberately no delete verb):

```bash
abstractgateway entity create --name Castor [--spark spark.yaml] [--data-dir ./runtime]
abstractgateway entity list
abstractgateway entity inspect Castor      # who it is, what it wrote, how it feels
abstractgateway entity card Castor         # the identity card: a page to know your companion
abstractgateway entity verify Castor       # both attestation chains + the spark hash
```

HTTP (rides the same auth as every `/api/gateway/*` endpoint):

| Method | Path | What it does |
|--------|------|--------------|
| POST | `/api/gateway/entities` | Create: lint the spark, store it verbatim, plant the identity core (`engram`), write the manifest. Idempotent for the same spark; a changed document is **refused** (409) — identity does not silently drift. |
| GET | `/api/gateway/entities` | List homes. |
| GET | `/api/gateway/entities/{name}` | Inspect: the folded identity core, recent diary gists, top standings, and the wake reasons — open questions (curiosity), open problems (something wrong), incubating ideas (direction). Pure reads — inspecting never counts as the entity *using* its memory. |
| GET | `/api/gateway/entities/{name}/card` | The identity card ("something to know our companion"): the engine compositor's sections — identity, age+context, current state (a window, never a point), likes/dislikes (G+ and G− separate; ambivalence preserved), open/resolved questions (resolution is the entity's own act), key moments, discoveries — each with provenance, plus gateway overlays (name/age, operator state, mind substrate, host moments). `?as_of=<seq>` anchors the card at a journal moment ("who was he at seq 500"). Pure reads. |
| GET | `/api/gateway/entities/{name}/verify` | Verify the book's hash chain, the graph projections against the book, the spark document against the engrammed marker, and the manifest. |
| POST | `/api/gateway/entities/{name}/summon` | Summon the entity into a work session (below). |
| POST | `/api/gateway/entities/{name}/chat/open` | Open a hosted conversation (the web chat backend; same turn loop as `entity chat`). Auto-yields the entity's own-time loop like `--pause-loop`; one live session per home; a refused prelude aborts verbatim. |
| POST | `/api/gateway/entities/{name}/chat/{chat_id}/turn` | One honest turn. `tools_ran` in the response is driver-authored data — what actually executed, never derived from the reply prose. |
| POST | `/api/gateway/entities/{name}/chat/{chat_id}/close` | End the visit: reflection pass (feelings move there), close summary, the own-time loop woken if the open yielded it. |
| GET | `/api/gateway/entities/{name}/chat` | Is a visit open on this home right now? (one life, one summon) |
| GET | `/api/gateway/entities/{name}/replay` | The observable life as a bounded stream (NDJSON): every memory-journal moment (formations, recalls, feelings, belief changes) plus gateway host markers (summons, refused preludes), in one strict sequence. |
| GET | `/api/gateway/entities/{name}/replay/stream` | The same stream as a live tail (SSE; `Last-Event-ID` resumes exactly). History scrub and realtime are one format — a viewer that renders one renders both. |

Progressive disclosure: `inspect` shows diary **gists** only. The verbatim
prose stays in the book and is fetched by the entity itself during a session
(`DIARY_READ`), never bulk-exported by inspection.

## Summoning

Summoning opens a work session *as* the entity:

1. The gateway renders the **identity header** ("summon prelude": who you
   are, your values in ordinal precedence, your recent diary lines, your
   standing feelings). This is a pure read.
2. **A refused prelude aborts the summon.** If the budget cannot fit the
   identity core, the request fails (409) with the reason verbatim — *"a
   truncated core is a different person"*. There is no fallback to a
   truncated header.
3. The run starts with the **reserved-seats posture**: identity is always
   present in the working set (`self_fraction > 0`), and presence never
   counts as use — the lifetime counters keep measuring lived experience.
4. The prelude leads the run's system prompt; the work brief is the prompt.

```bash
curl -X POST http://localhost:8080/api/gateway/entities/castor/summon \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt": "Review the backup strategy for the home lab."}'
```

The response carries the `run_id`, the session id, and the rendered prelude.
The run behaves like any other gateway run (ledger stream, waits, cancel).

## The deposit gate (actor by channel, never by payload)

Every write to an entity's home passes a door that derives the **actor from
the channel the request arrived on** — never from request payloads:

- `workplace` — summoned work sessions. Routine feelings only (small
  amplitudes); no identity writes; no belief retraction; no diary forgery
  (the diary is written through the entity's own elected act, `DIARY_WRITE`,
  whose author is bound at construction).
- `entity-reflection` — runs the home itself spawns (the future heartbeat /
  reflection loop). This is where identity evolves.
- `operator` — the authenticated admin surface (CLI, admin HTTP).

Mechanics: the summon endpoint signs a stamp (HMAC, per-data-root secret)
over the entity, channel, session, **and run id**; the routing layer verifies
it before any home opens. A payload claiming a privileged actor fails loudly.
The stamp authenticates "minted by this gateway" — remote workplace
authentication is the deferred key/signature work. This is the
[AI-fingerprints](https://medium.com/@lpalbou/the-rise-of-cognitive-architectures-and-the-need-for-ai-fingerprints-fcee286c0c33)
direction applied at the door: identity verified at the boundary, never
self-claimed.

The gate also enforces:

- **The privacy boundary**: a session's recall ladder may only contain the
  entity's own scopes plus that session's scope — never another user's.
- **The summon posture and the identity floor**: a recall budget that omits
  `self_fraction` gets the posture default; an explicit `self_fraction <= 0`
  is rejected ("identity is always present for a summoned entity"). Below
  the hard floor (5% and at least one reserved seat) nobody goes; reducing
  identity presence below the default — *hyperfocus*, a conscious and risky
  tradeoff — is the entity's own act (entity-reflection channel only,
  reversible by construction: per-session, never persisted). A stripped
  entity may act out of character; workplaces cannot request it.
- **Anchors**: journal time-travel anchors beyond this entity's own journal
  are rejected.
- **Verified participants**: who is present in the session is stamped by the
  door from the authenticated principal and flows into recall and formation
  (the situation contract) — payload claims are dropped.

## Observing a life (the replay stream)

The replay endpoints serve the memory engine's frozen stream (one envelope
shape; the journal is the stream) merged with gateway **host markers** —
moments that are deliberately invisible to the entity's own journal because
nothing happened memory-side (a prelude render is a pure read): summons,
refused preludes. Markers are gateway bookkeeping (like run ledgers), stored
outside the home directory, and take fractional sequence positions so they
interleave without ever colliding with the journal.

Privacy: diary content never enters the stream — the engine redacts diary
display blocks at the source (`{"redacted": "diary"}`), and every HTTP
consumer of these endpoints is a non-entity audience, so the topology of the
diary is visible (the entity wrote *something*, it connects to *something*)
while the words stay in the book.

### Reading a record's verbatim

`GET /api/gateway/entities/{name}/records/{graph_id}/verbatim` serves the
full stored text behind a memory record (the digest is prompt currency;
the verbatim is the lossless original). Three shapes:

- **Lived records** (episodes, notes): served from the home's artifact
  store, lossless.
- **Identity records** (values/purposes/traits): their verbatim IS the
  attested spark document — the endpoint serves the spark text itself.
- **Born-digest records** (interests, dreams): born as words — their
  digest is their complete text, never a compression. Served as-is with
  `born_digest: true` ("the words you see are all the words there are").
- **Diary projections**: refused (403) by multiple independent signals,
  plus a content backstop that refuses any verbatim carrying an
  unstripped diary block. The words stay in the book (operators use the
  diary door below).

### The operator diary door (reads are visible events)

`GET /api/gateway/entities/{name}/diary/{entry_id}?reason=...` serves a
book entry — private included — to the **operator** channel. This is the
maintainer's debugging ruling made honest rather than covert: the book
already lives unencrypted on the operator's machine; this door makes the
read *recorded* instead of silent. `reason` is required, and every
disclosure lands a `diary_read` host marker (entry id + reason) in the
entity's replay stream before the words return — the entity's biography
shows who read it and why. Failed lookups disclose nothing and are not
marked. The record-verbatim endpoint's structural diary refusal is
untouched: that surface is reachable from entity-adjacent contexts; this
one is the operator door.

## What can never be relaxed

- Never-purge and only-entity-writes are **structural** (absent code paths
  and construction-bound authorship), not policy checks.
- A refused prelude aborts the summon.
- Actor strings are made true at the door; everything downstream trusts them.

Design history: `a2a/threads/0004-gateway-entity-lifecycle/` (the charter,
both halves) and `a2a/threads/0003-named-persistent-identity/` in the
framework monorepo; reference implementation
`abstractruntime/tests/test_readoption_experiment.py`.
