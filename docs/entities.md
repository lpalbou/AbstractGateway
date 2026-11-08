# Summoned entities

The gateway owns the lifecycle of **summoned entities** — persistent
identities (like "Castor") that live across sessions, users, and workplaces;
each summon is a re-adoption of the same self. An entity is not a chatbot configuration: it is a home
directory holding everything the entity is and has lived, plus the lifecycle
surface to create, inspect, verify, and summon it.

This page uses human words first, API names in parentheses.

Who can see an entity: an admin sees every entity; anyone else sees only the
entities they created (recorded as `created_by` when the entity is created).
An entity you may not see answers like a missing one (404). An entity is a
member account (the two roles are admin and member).

Who configures an entity: its **creator** — mind, voice, tools per phase,
instructions, skills, workspaces, preferences, archive, unarchive and its
**Active** switch — within what the admin authorised (the models and voices
the gateway offers, the workspace caps, no tier-2 tool); an admin always can.
Another member gets "Only an admin or <entity>'s creator can …", and the
entity never changes its own settings. Sleep and wake, personal time, the work
order, memory review and the memory index rebuild stay an admin's. `GET
/api/gateway/entities/{name}/access` says what you may change. See
[security.md](./security.md#who-configures-an-entity).

In the consoles, entities are rows of the **Accounts** table, with an
**Active** switch that suspends and resumes them; they have no token to rotate
and no delete. See [security.md](./security.md#who-sees-which-account) and
[console.md](./console.md#accounts).

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
gateway's `manifest.json` (an internal record: the engraved owner key —
`entity:<name>`, or `entity:<name>@<home-id>` on homes created with that
form, kept for life because journals are append-only — plus creation time, the spark hash, and reserved fields for
the future key/signature work).

**The entity's ID — the handle — is `<name>@<gateway ip>`**, e.g.
`castor@192.168.1.146`: the name at its home
gateway's current LAN address (or the operator-declared
`ABSTRACTGATEWAY_DECLARED_ADDRESS`). That is what every operator surface
shows as "Entity ID". The manifest string above is a birth marker, never
the id; the address is never written at rest, so moving the gateway never
touches a record.

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

Through the running gateway's routes (`--url`, else the gateway running for this data dir;
`--token`, else `$ABSTRACTGATEWAY_AUTH_TOKEN`, else this data dir's bootstrap admin token for a
loopback URL; `--json` prints the route's payload; a refusal prints the gateway's sentence and
exits 2):

```bash
abstractgateway entity identity get Castor [--rev N]     # its look, voice, reference, fingerprint, age
abstractgateway entity identity set Castor --style watch-lotus --color "#3366cc" \
    --provider kokoro --model kokoro-82m --voice am_adam [--speed 1.1] [--quality-preset P]
abstractgateway entity record Castor [--track-record | --reputation | --acquaintances] [--since ISO]
```

`entity record` prints "No experience yet" where nothing was counted, and says what the numbers
mean: success is the work finishing (completion rate only); how good it was is not assessed yet.

HTTP (rides the same auth as every `/api/gateway/*` endpoint):

| Method | Path | What it does |
|--------|------|--------------|
| POST | `/api/gateway/entities` | Create: lint the spark, store it verbatim, plant the identity core (`engram`), write the manifest. Idempotent for the same spark; a changed document is **refused** (409) — identity does not silently drift. |
| GET | `/api/gateway/entities` | List homes. |
| GET | `/api/gateway/entities/{name}` | Inspect: the folded identity core, recent diary gists, top standings, and the wake reasons — open questions (curiosity), open problems (something wrong), incubating ideas (direction). Pure reads — inspecting never counts as the entity *using* its memory. |
| GET | `/api/gateway/entities/{name}/card` | The identity card ("something to know our companion"): the engine compositor's sections — identity, age+context, current state (a window, never a point), likes/dislikes (G+ and G− separate; ambivalence preserved), open/resolved questions (resolution is the entity's own act), key moments, discoveries — each with provenance, plus gateway overlays (name/age, operator state, mind substrate, host moments). `?as_of=<seq>` anchors the card at a journal moment ("who was he at seq 500"). Interest, lesson and key-moment briefs carry their [stored understanding](#a-records-stored-understanding-who-wrote-these-words). Pure reads. |
| GET | `/api/gateway/entities/{name}/tile` | What one small card about an entity shows, and nothing more (about 2 KB instead of up to 90 KB): name, look, age, mind (its model); what it is doing now (`now.phase`: sleep, visit, personal, work or stopped; `now.state`, the operator state under it — `awake` under `sleep` is the resting default; `busy`; `since`; `trouble: {kind: "own_time_failed"}` when its own time stopped after repeated failures); its newest interests (`into_lately`), its newest open problem or question (`on_its_mind`), its latest lesson, its last meaningful moment (`last_moment.kind` from a closed set: summon, session_closed, sleep, wake, pause, personal_started, personal_stop_requested, personal_frozen, phase_changed, substrate_changed, night_voice, work_order_changed, task_inbox_changed, identity_changed) and counts. The roster (`GET /entities`) advertises it as `reads.tile`; a client that does not find it there reads the card. Pure read; a source that cannot be read costs its facts with a labelled warning, never the tile; a held home answers 409. |
| GET | `/api/gateway/entities/{name}/verify` | Verify the book's hash chain, the graph projections against the book, the spark document against the engrammed marker, and the manifest. |
| POST | `/api/gateway/entities/{name}/summon` | Summon the entity into a work session (below). |
| POST | `/api/gateway/entities/{name}/chat/open` | Open a hosted conversation (the web chat backend; same turn loop as `entity chat`). Auto-yields the entity's own-time loop like `--pause-loop`; one live session per home; a refused prelude aborts verbatim. The gateway never caps the entity's output: the model works at its full capacity (the former `max_output_tokens` field is ignored and the response then carries a one-line `deprecation`). |
| POST | `/api/gateway/entities/{name}/chat/{chat_id}/turn` | One honest turn. `tools_ran` in the response is driver-authored data — what actually executed, never derived from the reply prose. |
| POST | `/api/gateway/entities/{name}/chat/{chat_id}/close` | End the visit: reflection pass (feelings move there), close summary, the own-time loop woken if the open yielded it. |
| GET | `/api/gateway/entities/{name}/chat` | Is a visit open on this home right now? (one life, one summon) |
| GET | `/api/gateway/entities/{name}/replay` | The observable life as a bounded stream (NDJSON): every memory-journal moment (formations, recalls, feelings, belief changes) plus gateway host markers (summons, refused preludes), in one strict sequence. |
| GET | `/api/gateway/entities/{name}/replay/stream` | The same stream as a live tail (SSE; `Last-Event-ID` resumes exactly). History scrub and realtime are one format — a viewer that renders one renders both. |
| GET | `/api/gateway/entities/{name}/track-record`, `/reputation`, `/acquaintances` | What it has done (runs, successes, failures, tokens, per kind of task), how far it can be trusted per kind of task (with how many outcomes that rests on), and the entities and humans it met. Derived from its home's records; nothing is stored. See [API: track record, reputation and acquaintances](./api.md#track-record-reputation-and-acquaintances). |

### The entity's mind and voice

| Method | Path | What it does |
|--------|------|--------------|
| GET | `/api/gateway/entities/{name}/substrate` | Its mind: its own choice (`provider`, `model`, `thinking`, `speculation`; null when it has none), `gateway_default` (the gateway's text route), `effective` (what its next visit thinks with) and `source`: `entity`, `gateway`, or `unset` (the gateway has no text model; `note` says what to do). |
| GET | `/api/gateway/entities/{name}/access` | May you change its settings? `{entity, can_configure, as: "admin" \| "creator" \| null, reason, admin_only_tools: {tools, reason}, admin_only: {available, reason}}` — `reason` is the sentence to show on a control you can't use. |
| PUT | `/api/gateway/entities/{name}/substrate` | An admin or its creator (the creator: a model the gateway offers, else `403 not_offered` naming the offered models). `{provider, model, thinking?, speculation?}` sets its own mind; `{"clear": true}` returns it to the gateway default. Each change is recorded in its history (`substrate_changed`). `speculation` is MTP: `false` (off) or `{"mode": "native_mtp", "num_draft_tokens": N}`. |
| GET | `/api/gateway/entities/{name}/cognition-profile` | What each part of its thinking uses, and its memory settings (below). `capabilities.{summaries,understanding,extraction}`: `{choice, effective, source}`, where `choice` is `{"inherit": "mind"}` or `{provider, model, thinking, max_input_tokens}`, `effective` is `{provider, model, thinking}` (null only when it inherits and the gateway has no text model) and `source` is `profile` or `mind`; `recall: {shelf_size, source, applies_to_running_loop: "on restart"}`; `rebuild: {sensitivity, cap_per_turn, source, sources}` (`source` per block, `sources` per field: `profile` or `default`); `mind: {provider, model, thinking, source}` (`entity`, `gateway`, or `unset` with a `note`). |
| PUT | `/api/gateway/entities/{name}/cognition-profile` | An admin or its creator (the creator: a changed capability must use a model the gateway offers, else `403 not_offered`). Any subset of `{capabilities: {cap: {"inherit": "mind"} \| {provider, model, thinking?, max_input_tokens?} \| null}, recall: {shelf_size: int \| null}, rebuild: {sensitivity: number \| null, cap_per_turn: int \| null}}`: what is absent keeps its value, `null` returns it to its default. Refused with a sentence (400) outside the ranges below, for an unknown field, or for an `endpoint:` provider that is not an enabled connection. Each change is recorded in its history (`cognition_profile_changed`, old and new) before the file changes; the answer is the GET shape. |
| GET | `/api/gateway/entities/{name}/identity[?rev=N]` | Its digital identity (framework ADR 0052): `{entity_id, identity: {rev, avatar: {style, color, source, watch_code}, voice, set_at, set_by}, identity_ref, fingerprint, age: {born, days}}`. `?rev=N` serves an earlier revision from the home's biography. `identity.change` is why that revision was written: `{cause, written_by, at}` from its biography row (`cause`: `recorded_default`, `chosen`; null when the home has no row for it). A home without a record yet gets its recorded default here (unless it is closed for maintenance: 409). |
| PUT | `/api/gateway/entities/{name}/identity` | An admin or its creator: `{avatar: {style, color}, voice: {provider, model, voice, speed?, quality_preset?}}`, the whole identity. Refused in this order, for every role: the home is closed for maintenance (409); the design is not in this gateway's avatar catalog or cannot be drawn on the Watch (400, the designs listed); the voice is not one the caller's voice picker lists (`GET /voice/voices?provider=` as the caller; 400: "Castor can't speak with zz: it isn't a kokoro voice Castor can use. An entity speaks with this gateway's shared voices and its creator's own voices; another person's own voice stays theirs until an administrator shares it with everyone. Choose one of: …"); its model is not downloaded (409, `error.download`: "Castor's voice needs the kokoro model kokoro-big (330 MB), which is not downloaded on this computer yet. Download it (the Models page, or: …), then save the identity again."). Never cleared. Each change is `rev + 1`, recorded in the home's biography and as an `identity_changed` host marker. |
| GET/PUT | `/api/gateway/entities/{name}/voice` | Its voice: `{provider, model, voice}` or `{"clear": true}`; unset, it speaks with the gateway's default voice (`effective`). PUT: an admin or its creator (the creator: a voice the caller's picker lists, else `403 not_offered`). On a home with an identity record: served from the record (`source: identity`, never the gateway default in its place), a PUT changes the record's voice through the identity's checks, and `clear` is refused (409). |
| GET/PUT | `/api/gateway/entities/{name}/appearance` | How apps draw it (the colour and shape of its orb): `{color: "#rrggbb", shape: planet\|kitty\|face\|wave}`, the whole look replaced, or `{"clear": true}`; unset, apps use their own look. Also accepted at creation (`appearance` on `POST /entities`) and returned on each row of the list. On a home with an identity record: the record's colour and the shape its design draws (`watch-shape-<shape>`, else `planet`; `source: identity`); a PUT changes the record's look (what is not given keeps its value; the voice is kept), `clear` is refused (409), and a record with no voice yet refuses a look change with its sentence. |

**The digital identity.** Every entity has one record in its home, `identity.json`
(AbstractRuntime's format): an avatar design from the abstractavatar catalog, its
colour, and its voice. An entity born now gets its identity at birth: the one
`POST /entities` names in `identity` (checked before anything is created), else the
recorded default (a design and colour computed from its id, the gateway's default
voice as a value, or no voice when the gateway has none); a birth `appearance` becomes
the default's look (a chosen look needs a voice: refused before creation when the
gateway has no default voice). Each existing home without a record gets the recorded
default once when the gateway starts (a home closed for maintenance is skipped and
logged, a record is never overwritten), else at its first `GET /identity`. A later
change of the gateway's default voice never changes a recorded identity. The entity
speaks with the record's voice only: its TTS lanes answer `voice_source: identity`,
`identity_ref` and the `X-Identity-Ref` header (`entity:castor#3`), and a record with
no voice refuses with "Castor has no voice yet: choose one in its Identity settings
or set the Gateway default voice". The reference also rides each visit
(`_runtime.identity_ref`, beside the signed stamp, and on the open response), each
meet leg, relay row and status row, each public update row of a visit, and the visit's state (`GET /visit/{run_id}/state`, top-level `identity_ref`); a home
without a record carries none. The list rows carry `identity` (the record with its
`watch_code`, or null). Visits, meet legs and summons record who started them
(`_meta.origin`: kind `person`, actor the same `person:<user>` as the participants).

**Whose voices an entity may speak with.** The voice checks (identity, the legacy
voice door, the identity at birth, a conversation's identity) list the voices exactly
as the caller's voice picker does: `GET /voice/voices?provider=` as the caller, in
their runtime plane. On a gateway with user accounts that is the shared library (the
built-in voices and the ones an administrator made or shared) plus the voices of the
account whose plane holds the entity: its creator's own voices, cloned or designed
(an administrator acting on someone's entity is served by that plane). An entity
lives and speaks in its creator's plane (every route that makes it speak runs
there), so a creator's own voice is heard by everyone who meets it; if the creator
deletes that voice, the entity cannot speak with it any more until another voice is chosen.
Another person's own voice is not in that plane and is refused with that sentence;
an administrator can share it with everyone first
(`POST /api/gateway/voice/clones/{voice_id}/deploy`). A conversation's identity
speaks with the shared voices and your own.

An entity without its own mind thinks with the gateway's text route (the
console's text default, with its endpoint and reasoning); a request may still
name a provider and model for one visit. There is no environment variable for
any of this. The console sets both in **Manage → Mind & voice**, the Entity app
in **Settings → mind / voice**.

**The cognition profile.** Beside its mind, an entity has a cognition profile
(`cognition.yaml` in its home, AbstractRuntime's format):

| Setting | Default | Range |
|---|---|---|
| `capabilities.summaries` (summaries and memory details), `capabilities.understanding` (understanding and dreams), `capabilities.extraction` | the mind | `{"inherit": "mind"}` or `{provider, model, thinking?, max_input_tokens?}`; `thinking` one of `none, minimal, low, medium, high, xhigh, auto, on, off`; `max_input_tokens` 4096 to 140000 (140000 when not given) |
| `recall.shelf_size`: memories retrieved per turn, in visits and in its own time | 50 | whole number 1 to 400 |
| `rebuild.sensitivity`: how easily a new memory or a surprise makes it revisit what it already understood | 0.5 | number 0 to 1 |
| `rebuild.cap_per_turn`: memory rebuilds a turn may run at most (0 = only while it rests) | 3 | whole number 0 to 10 |

Talking always uses the mind. A visit (`chat/open`), a durable visit, a summon
and the start of its own time use `recall.shelf_size` when the request names no
`shelf_size`; a request's own value still wins. A running own-time day reads
the width when it starts: a change applies from its next start, including a
restart after a crash.

Progressive disclosure: `inspect` shows diary **gists** only. The verbatim
prose stays in the book and is fetched by the entity itself during a session
(`DIARY_READ`), never bulk-exported by inspection.

### The task inbox (tasks left with an entity)

Tasks are durable facts in the home — append-only events in
`<home>/task_inbox.jsonl`, folded at read (state is never rewritten in
place; two writer processes exist — the door and the own-time loop — so
every write is one flock-guarded appended line):

| Method | Path | What it does |
|--------|------|--------------|
| GET | `/api/gateway/entities/{name}/tasks` | The folded inbox: tasks with current status (`pending\|taken\|done\|parked`), chronological. `exists: false` when no inbox was ever created. |
| POST | `/api/gateway/entities/{name}/tasks` | Leave a task (admin): `{title, brief?, workflow?, backlog_ref?}`. `origin`/`by` are STAMPED from the authenticated principal — the body carries no origin field to forge. Marker-first (`task_inbox_changed`); a marker failure refuses the write. |
| POST | `/api/gateway/entities/{name}/tasks/{task_id}/status` | Advance a task (admin): `{status, note?}`. Validated before the marker; unknown tasks 404. |

A visit can leave tasks at close: `POST .../visit/{run_id}/close` accepts
`tasks: [{title, brief?, …}]`, recorded with origin `visit:<run_id>` only
after the close COMPLETED (recording failures surface as a labeled warning
in the response — the finished close is never misreported as a 5xx).

### Skills (what an entity is taught)

Beyond the always-verbatim capability map, an entity's skills are an
operator SELECTION in the home (`<home>/skills.yaml`: `[{name, phases?}]`,
phases from the ruled four, absent = selected everywhere), resolved
server-side against the abstractskill shelf through the same trust gate as
every other lane — default-requested, never trust-bypassed:

| Method | Path | What it does |
|--------|------|--------------|
| GET | `/api/gateway/entities/{name}/skills` | One resolved truth for every UI: the stored selection, roster rows (name, description, trust_level, requires_review, tree_hash, source) with labeled verdicts for anything unresolvable, and the capability-matrix payload (a global selection renders all four phases with identical cells). |
| PUT | `/api/gateway/entities/{name}/skills` | Replace the selection (an admin or its creator). Marker-first (`skills_selection_changed`, old/new names+phases — never skill bodies); the response is the resolved view so a typo or blocked skill is visible the moment it is written. |

`POST /api/gateway/entities` accepts the same selection at **birth**
(`skills: [{name, phases?}]`) so a new entity carries teaching from day
one. Selections pin by NAME and resolve to the current shelf state at
read/summon time — a shelf re-pin reaches homes on their next resolution,
never by bulk push. Delivery into entity prompts (progressive disclosure:
names ride the base, bodies activate on demand) waits on the runtime's
composition-slot election; the selection file rests in the home until then.

The entity roster (`GET /entities`) carries `pending_tasks`
render-when-present: the field exists only for homes that have an inbox —
an entity never handed a task shows no field, not a zero. The file schema
is the cross-package contract for the runtime's day-open reader (the R-C
loop half): event lines `{"event": "added"|"status", "task_id", …}` — see
`abstractgateway/entity_tasks.py` for the authoritative shapes. The inbox
records facts; whether a task opens the work phase is decided by the entity's
loop.

## Drive ratios (cognition health)

`GET /entities/{name}/cognition` carries a `drives` block — memory's
`cognition_health()` fold over the home's full ladder: questions
open/resolved, problems open/repaired, interests open/explored, each with
a `ratio` that is `null` when the category is empty (a life with no
questions has *no* ratio, never a fabricated 100%). Ratios are data —
never-100% is the design (an entity with nothing open has no pull
forward), so consoles render an amber cue at saturation, not a success
state. Render-when-present: the key is absent (with a labeled `#FALLBACK`
warning) when the engine predates the read or the read fails.

The roster (`GET /entities`) carries the same `drives` block for **warm
homes only** — homes already open in this gateway process. The roster is
deliberately file-cheap and never opens a store; a cold home shows no
field (absent ≠ zero), and `/cognition` always serves the block (and
warms the home). The gateway console renders the two ratio bars in the
entity Overview panel from the same `/cognition` read.

## Live conversation during a visit

Open `POST /api/gateway/entities/{name}/visit/open` with a stable `session_id` and
optional `conversation: true` (a strict JSON boolean) to enable public updates and
conversational questions. The mode is fixed before the first tick; rejoining an existing
managed session with a conflicting mode is refused (422). Existing tool policy remains
separate; `ask_user` is an interaction primitive in this mode even when external tools are off.

Read `GET /api/gateway/entities/{name}/visit/{run_id}/state`. In conversational mode it adds:

- `public_updates`: up to 64 explicit, safe actor updates with `id`, `text`, `run_id`,
  `session_id`, `iteration`, `source_call_ids` and `update_kind` (`"finding"` or `"progress"`,
  as typed by the actor).
- `public_messages`: up to 64 accepted human corrections with `role: "user"`,
  `kind: "guidance"`, `id`, `text`, `run_id`, `session_id`, `speaker` and
  `guidance_status: "accepted"`.
- `public_questions`: up to 64 canonical ASK_USER interaction messages: assistant
  `question`, user `answer`, system `question_outcome` for expiry, and user
  `retained_reply` with `reply_status: "retained"` and its reason. Question/answer rows retain
  their actual `wait_key`; `question_id` comes from the original wait when available.
  Expiry retains `question.outcome: "expired"`; retained replies keep the original question id.
  The server selects only known ASK_USER sources and trusted host outcomes, screens diary/thinking
  fences, and never serves generic home messages or reasoning as question history.
- `public_updates_omitted`, `public_messages_omitted`, `public_questions_omitted`, `conversation_runs_omitted`:
  explicit counts when this bounded view omits entries or descendants; the full ledger remains.
  Question omissions include rows withheld by privacy-fence screening. Clients show a history
  omission notice when any count is positive.
- `pending_question`: null or the actual question wait, including its child `run_id` when
  applicable, `wait_key`, prompt, choices, `allow_free_text`, `until` and `details.question`.
  When present it is also the `pending_wait`. All pending waits carry their actual `run_id`.

A permission `pending_wait.details.public_action`, when present, carries the actor’s
explicit public explanation (`text`, `update_id`, `run_id`, `iteration`, `call_ids`).
It is bound to the exact pending batch and narrowed when the session tool policy
withholds actions. It grants no permission and does not prove execution. A missing
explanation must not be reconstructed from private thoughts or another update.

This is a participant-facing projection over the owned visit and verified descendants,
bounded to 32 runs. Clients must not read the entity home's raw ledger or diary to render it.
Explicit public events still undergo server-side diary/thinking-fence screening; an event
marker alone does not make private content public.
All three public message arrays retain canonical `order: [timestamp, run_id, ledger_seq]`
and `published_at` (the same timestamp, from the record's end or start time). They are
chronologically ordered before the 64-entry cap. Clients merge the arrays by this order;
grouping all human input before actor updates would change the history.

While the entity is working in its current turn, send
`POST .../visit/{run_id}/guidance` with `{text: "quoted context and correction",
display_text: "the person's original words"}`. `display_text` is optional; this authenticated
human visit route uses `text` when it is absent. Display words must be nonblank and at most
65,536 characters. The server derives the participant identity; clients cannot choose it.
The response `{accepted: true, seq, run_id}` means durably queued, not adopted. Idle,
paused or currently waiting visits refuse live guidance (409); send the next turn or answer
the actual wait instead. Accepted corrections are recalled and attributed before the next
reasoning boundary, with no additional model call for empty queues. Old-turn corrections
remain retained instead of silently becoming instructions for a new turn.

Answer a conversational question through `POST .../visit/{run_id}/answer` with
`{wait_key: "question:<id>", response: "the person's words", target_run_id?: "<actual child>"}`.
An omitted target selects the actual pending question; an explicit target must be a verified
descendant in the same owned visit. Empty text, extra approval fields and tool-approval waits
are refused; this route does not approve tools. It returns `{accepted, run_id, target_run_id}`
with the refreshed safe conversation projection. Paused visits refuse answering without
continuation. Late recognized replies are retained under Runtime's question contract and
cannot answer a newer question. Essential questions have no deadline; timed clarifications
apply only their predeclared safe fallback. See [api.md](api.md#conversational-updates-questions-and-corrections).

Tool approvals keep their existing `/decision` route and exact pending key; they never time
out into approval. A managed visit's authenticated owner is checked before these routes;
wrong-owner requests do not disclose it. Generic run steering remains refused for these
driver-owned visits. A Flow summoned work session is a separate ordinary Gateway run: it
uses `/runs/{root}/view` and standard commands, not the driver visit routes above.

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

Privacy: the engine redacts diary display blocks at the source
(`{"redacted": "diary"}`). These HTTP endpoints serve the OPERATOR audience,
so the serving end resolves that marker into the entry's **gist** — the
entity's one-sentence summary, or the first ~120 characters when no explicit
gist exists (`_operator_diary_display`).
So the operator sees the diary's topology (the entity wrote *something*, it
connects to *something*) **and a gist of what it was about** — never the full
verbatim prose, which stays in the book and is fetched one entry at a time
through the operator diary door (`GET .../diary/{entry_id}`, a marker-first
recorded read). The redaction marker itself is never served raw.

### Reading a record's verbatim

`GET /api/gateway/entities/{name}/records/{graph_id}/verbatim` serves the
full stored text behind a memory record (the digest is prompt currency;
the verbatim is the lossless original). Three shapes:

- **Lived records** (episodes, notes): served from the home's artifact
  store, lossless.
- **Identity records** (values/purposes/traits): their verbatim IS the
  attested spark document — the endpoint serves the spark text itself.
- **Born-digest records** (interests, dreams, world-model cards, lessons
  formed without a verbatim): born as words — their digest is their
  complete text, never a compression. Served as-is with
  `born_digest: true` ("the words you see are all the words there are").
- **Summaries without a verbatim** (older session reflections, the
  mechanical floor): served with their digest as the text,
  `born_digest: false` and `summarizes` (the graph ids of the records the
  summary covers). Any other record without a verbatim is an honest 404.
- **Diary projections**: served to the **operator** from the book. The read
  is marker-first: a `diary_read` host
  marker (entry id, kind, visibility) lands in the entity's replay stream
  before the words return, so the disclosure is recorded in the entity's
  biography. Private entries are included. Born-digest diary kinds
  (interest/dream) serve their digest as the verbatim.

### A record's stored understanding (who wrote these words)

AbstractMemory rewrites a record's words (its digest) in place and keeps
every earlier reading as a version. The gateway serves what memory stores,
under memory's own names, so an app can say "Summary by Nerin's mind
(qwen3.8-27b), 7 Nov" or "Extract (mechanical)" without guessing from the
text. Every block carries all of these keys; a value the store does not
hold for the record is `null`, never a missing key:

| Field | Meaning |
|---|---|
| `digest` | The words of the reading |
| `digest_method` | How they were made (`mechanical-v2`, `mechanical-card-v2`, `authored-v1`, ...) |
| `digest_version` | Which reading (0 = the original) |
| `version_count` | How many readings memory keeps (0 = never rewritten) |
| `authored_by` | `{kind, id}`: `mechanical`, `entity-mind`, `capability-model`, `operator` (or `unrecorded` for an old label) |
| `reader` | `{provider, model, thinking}` of the model that wrote the words (null for mechanical and operator text) |
| `understood_at` | When this reading was formed |
| `cause` | Why it was written: `formation`, `backfill`, `ripple`, `evidence`, `operator`, `revert` |
| `target`, `target_type`, `card_revision`, `source_count` | World-model cards: who or what the card is about, its revision, how many records it was built from |
| `narrated`, `narration_version` | Dreams: whether a narration is stored beside the summary |
| `replaced_by` | A superseded record: the graph ids of the records that replaced it |

Where the app reads them:

- **The record read** (`GET .../records/{graph_id}/verbatim`): every
  non-diary answer carries the fields of the record's current reading and
  `versions`, memory's list of every reading, oldest first (each with
  `version`, `text`, `digest_method`, `author`, `reader`, `cause` as
  `{cause, ...}`, `understood_at`, `seq` and `current`; empty when the
  record was never rewritten).
- **Recall snapshots** in the replay stream (`family: "snapshot"`, with
  `enrich=true`): `display.recalled` holds one block per recalled record
  (`record_id` as the snapshot lists it, `graph_id`, `kind`, then the
  fields). The words and their provenance (`digest`, `digest_method`,
  `digest_version`, `authored_by`, `reader`, `understood_at`, `cause`) are
  the reading that entered that context, so `digest` equals the
  snapshot's own row; `version_count`, `replaced_by` and the card and dream
  fields describe the record now. The payload (the journal record) is
  unchanged.
- **The card** (`GET .../card`): each record brief in
  `discoveries.interests`, `lessons.lessons` and `key_moments.moments`
  carries the fields of the current reading (its `statement` is the same
  words as `digest`).

Diary records get none of this: their record read is the book entry as
before (a recorded `diary_read`), recall snapshots do not list them in
`display.recalled`, and the card's questions, problems and book lessons are
unchanged. The reads deposit nothing in the entity's memory.

### The operator diary door (reads are visible events)

`GET /api/gateway/entities/{name}/diary/{entry_id}?reason=...` serves a
book entry — private included — to the **operator** channel. The book
already lives unencrypted on the operator's machine; this door makes each
read *recorded* rather than silent. `reason` is **optional** (default
"operator review"; the identity, the act and the timestamp are the audit
record), and every disclosure lands a `diary_read` host marker (entry id +
reason) in the entity's replay stream before the words return — the
entity's biography shows who read it and why. Failed lookups disclose
nothing and are not marked. Both full-content doors (this one and the
record-verbatim endpoint above) serve the operator; the boundary that
stays closed is the *effect* layer (a workplace channel cannot read the
book), not the operator's HTTP surface.

## What can never be relaxed

- Never-purge and only-entity-writes are **structural** (absent code paths
  and construction-bound authorship), not policy checks.
- A refused prelude aborts the summon.
- Actor strings are made true at the door; everything downstream trusts them.
