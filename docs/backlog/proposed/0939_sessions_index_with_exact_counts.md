# 0939-abstractgateway: [FEATURE] A sessions index: every conversation with its exact turn and tool-call counts in one request

> Package: abstractgateway (route and index), abstractruntime if the run store must keep the aggregate
> Type: feature
> Created: 2026-10-26
> Priority: P2
> Labels: sessions, runs, clients, performance

## Summary

`GET /api/gateway/sessions` listing the caller's conversations (root-run sessions), newest first,
each with its id, title, kind, first and last run time, archived flag, and exact `turns`,
`tool_calls` (and, when cheap, `tokens_total`), paged, without the clients having to fetch and
group runs. The counts are kept by the gateway as runs complete, not computed by scanning the run
store on every request.

## Why

Operator, 2026-10-26, on the iPhone's conversation list showing "1+" everywhere: "why do we have
1+? I would prefer the exact numbers if it doesn't slow down the gateway." Today no route lists
sessions: every client fetches `GET /runs?root_only=true` with a cap (60 on the iPhone, the watch's
own window) and groups the runs by `session_id` itself. When the window is full the counts are
lower bounds, so the iPhone writes "1+" ("at least"), the watch shows the window's count as if it
were the whole, and the desktop keeps a local registry. A run summary is a large object; raising
the window to make the counts exact makes every refresh heavier on the radio and the gateway. A
sessions index is small (one row per conversation) and exact.

## Current code reality

- `GET /runs` (`src/abstractgateway/routes/gateway.py` ~9918) lists runs with `include_metrics`
  (steps, llm_calls, tool_calls, tokens_total per run); `root_only`, `archived_only`,
  `session_id` filters; `include_drafts` hides draft-test runs. No session listing exists; the
  session routes are archive/unarchive, search, artifacts, history bloc, workspaces, prompt cache.
- Clients: the watch groups `recentRuns(limit:)` into `SessionSummary` (turns = rows in the
  window, tool calls summed from the rows' metrics); the iPhone does the same from 60 runs and
  marks the counts partial when the window is full (`AssistantStore.recentHistoryMayBePartial`).
- The session's title is a tag on the session (backlog 0937 proposes writing it with a model).

## Proposed direction

- Keep per-session aggregates where the gateway records a run's end (turns, tool calls, last
  run time, first run time), so the list is a read of small rows; backfill once from the run
  store at upgrade (a one-time scan).
- `GET /api/gateway/sessions?limit=&cursor=&archived=` → `{sessions: [{session_id, title,
  session_kind, first_at, last_at, archived, turns, tool_calls, tokens_total?, last_run_id,
  last_status}], next_cursor}`. Draft-test sessions hidden unless `include_drafts`, like runs.
- `GET /runs` stays for a session's turns and for the activity views.
- Clients drop the grouping: the iPhone's "+" and the watch's silent undercount go away; one
  request per refresh instead of a 60-run page.

## Non-goals

No change to the run summaries. No search (the search route stays). No summaries or topics
(assistant backlog 0006).

## Promotion criteria

- The gateway seat agrees on where the aggregate lives (the run store's session record, or a
  gateway-side index), and a client (the iPhone) adopts it as the proof.

## Validation ideas

- Counts equal a full scan of the session's root runs on a seeded store; a 200-conversation store
  lists in one request well under 100 ms.
- The iPhone's conversation rows show exact numbers with no "+" and the watch's rows match them.
