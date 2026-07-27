# 0092 — Per-entity SESSIONS listing for admin surfaces (runs ≠ sessions)

- Status: proposed
- Date: 2026-07-26
- Origin: operator report on the console TUI Runtimes screen ("selecting a
  runtime doesn't get you access to the associated sessions?") — verified
  against the live gateway and `admin_runtimes.runtime_runs`.

## Problem

Selecting an entity plane in an admin surface (TUI Runtimes screen, and the
web console would hit the same wall) drills into
`GET /admin/runtimes/entity/{tenant}/runtime_{slug}/runs` — which correctly
reads the home's `runtime_<slug>.sqlite3` run store, and correctly answers
**empty** for entities that have lived plenty: entity CONVERSATIONS run
through the hosted chat lane (in-memory `ChatSession` → home handlers, no
RunState) and the life loop (process loop, not `Runtime.tick`), neither of
which creates runs. Only durable VISITS and summoned workflows land in the
home run store.

So the operator's mental model — "the entity's sessions" — has **no listing
endpoint at all**. What exists today:

- `GET /entities/{name}/visit` — the CURRENT durable visit only.
- `GET /entities/{name}/chat` — the CURRENT hosted chat session only.
- The replay stream / host markers (summon / session_closed) — the durable
  record of every session boundary, but an event stream, not a list.

## Proposal

`GET /api/gateway/entities/{name}/sessions?limit=N` — a bounded,
newest-first session list folded from what the home already records:

- host markers (summon / session_closed / visit open-close pairs) give
  session boundaries, channel (chat / visit / own-time day), and close
  reason;
- the chat ledger on `home.sqlite3` gives turn counts per session;
- durable visit runs (when present) give run_id + status so admin surfaces
  can deep-link to ledger/transcript endpoints that already exist.

Row shape (suggestion): `{session_id, channel, opened_at, closed_at|null,
turns, close_reason|null, run_id|null}`. Pure read; no store writes; diary
redaction untouched (session rows carry no content).

## Consumer

Console TUI 0.4.1 shipped the honest interim: the entity-plane runs panel
explains WHY it is empty ("entity chats/life days don't create runtime
runs"). When this endpoint lands, that panel swaps to rendering the real
session list for entity planes (runs list stays for default/user planes).
