# 1166-gateway: [FEATURE] Session kinds for this release: `automation` and `process` join `conversation`, `docs`, `notes`; probes are processes; visits are conversations

> Package: abstractgateway (`session_kinds.py`, `POST /runs/start` kind validation, the run listing's `session_kind` filter and rows, `admin_runtimes.py`), then the clients that start such sessions (AbstractAssistant phone/watch probes, AbstractNote processors, the automation controller) and the lists that show them (Code sidebar, Observer, Assistant switcher)
> Type: feature
> Status: planned
> Created: 2026-11-08
> Priority: P1 (operator: part of the current release)
> Labels: sessions, kinds, listing, observer, code

## Operator ruling (2026-11-08 11:45)

"I would keep conversations, docs, notes, automations. PROCESSES (a type of internal automations, not created by the
users, but necessary for our treatments, e.g. note processing). Probe: what do you use it for? Visit… isn't it just a
conversation but with an entity?"

Adopted: the kind set is `conversation`, `docs`, `notes`, `automation`, `process`.
- `process` = internal work the framework runs for its own treatments, never created by the user as such: AbstractNote's
  processor/reflect/consolidate occurrences (today marked `notes` by workflow), model-host verifications, the phone
  and watch voice probes (`session_memory_watch-voice-probe-*`, `model-host-verification-*`). A probe is a process; it
  gets no kind of its own.
- `automation` = an occurrence of an automation the user created (the automation controller's sessions).
- An entity visit is a `conversation` whose participant is an entity; the row names the entity. No `visit` kind.
- Free user tags: not in this release (the operator did not ask; recommend later as a separate item).

## Current code reality (2026-11-08)

`session_kinds.py`: `KINDS = ("conversation", "docs", "notes")`, a marks file per principal
(`<data_dir>/session_kinds.json`), `start_kind` at `POST /runs/start` (a non-conversation kind needs a `session_id`),
`kind_of` by the root workflow at read time, one-time migrations for docs-qa and abstractnote bundles. The turn listing
defaults to conversations; `session_kind=` lists others; Observer passes `session_kind=auto`. Root 1063 (typed runs and
sessions) is the full design; this item is its slice for the release.

## Scope

1. `KINDS += ("automation", "process")`; `kind_of` maps: the automation controller's workflows → `automation`; Note's
   processor/reflect/consolidate occurrence workflows, model-host verification and voice-probe workflows → `process`
   (by bundle/workflow id, exact, never text). A one-time migration marks existing sessions the same way (recorded in
   the marks file like `docs_qa_bundle_v1`).
2. Clients that open such sessions send the kind on `POST /runs/start`: the Assistant phone and watch probes send
   `process`; the automation controller sends `automation` for occurrence sessions; AbstractNote already lands on
   `notes` by workflow (its processors become `process`).
3. Every listing row carries `session_kind`; `session_kind=` accepts a comma list; `auto` keeps its meaning. The
   default stays conversations.
4. Code sidebar and Observer: a filter chip per kind that exists in the data (hidden when a kind has no sessions); the
   Assistant switcher unchanged (conversations and automations tabs already).
5. Tests: a probe session is listed under `process` and never under conversations; a visit session lists as a
   conversation with the entity named; the migration is idempotent; removing the mapping turns the tests red.

## Out of scope

Free tags, the surface (desktop/iPhone/watch) stamp of root 1063, Continuum's hub sessions.

## Size

Gateway ~3 h (kinds, mapping, migration, listing, tests); Assistant probes ~1 h; Code and Observer chips ~1 h each.
