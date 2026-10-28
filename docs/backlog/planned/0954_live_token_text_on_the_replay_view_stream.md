# 0954-gateway: [FEATURE] Live token text on the replay view stream

> Package: abstractgateway (`routes/replay_view.py` `/view/stream`, `live_deltas.py`); abstractruntime 0859 (the reducer ignores ephemeral deltas)
> Type: feature (regression of the view migration)
> Status: planned (not implemented)
> Created: 2026-10-28
> Priority: P1 (high)
> Labels: replay, sse, streaming, latency, multi-client
> Root link: AbstractFramework backlog 0993 ([relative link in a monorepo checkout](../../../../docs/backlog/planned/0993_one_ledger_replay_many_views.md)) and 1065 (client migrations); AbstractRuntime 0854 (replay view), 0859; completed 0947 (view routes)

## Summary

Finding from the AbstractAssistant desktop migration (draft PR #11, `feat/replay-view-desktop`, its backlog 1063): the `/view/stream` deltas carry committed ledger records only, so a client that renders from the view loses the live token text while an answer is being generated, a regression against the old path (`GET /runs/{id}/ledger/stream`), which interleaves `llm.delta` frames from `LiveDeltaHub` (`live_deltas.py`) and shows tokens as they arrive. Runtime 0854 put live token text out of scope ("`llm.delta` frames stay on the ledger stream"); the migration shows the view stream needs it.

## Current code reality (origin/main 8369e5fb)

- `routes/replay_view.py`: `GET /runs/{id}/view/stream` sends `event: view`, then `event: delta` per fold (`id:` = cursor token), then `done`; no live text.
- `live_deltas.py`: `LiveDeltaHub` per root run, frames `{kind: "llm.delta", run_id, parent_run_id, node_id, call_id, seq, text, channel}` and `llm.delta_end`; snapshot of open calls on subscribe; split deployments tail `<data dir>/live/<root>.deltas.jsonl`. Served today only by `/runs/{id}/ledger/stream` (`routes/gateway.py` ~11645-11782). Nothing durable: the LLM_CALL record is written before `llm.delta_end`.

## Chosen route (proposal; option A preferred)

- **A. Ephemeral `text` event on `/view/stream`**: the route subscribes to the same `LiveDeltaHub` scope as the ledger stream and sends `event: text` frames `{run_id, node_id, call_id, channel, seq, text}` (appended text) and `text_end`, never persisted, never given an `id:` (so `Last-Event-ID` resumption is by the view cursor only; on reconnect the hub's open-call snapshot restores the text so far). The committed message record that follows supersedes the ephemeral text: the client drops a call's live text when a view delta brings that call's message. The reducer never sees these frames (runtime 0859).
- **B. Interim documented rule**: until A ships, clients keep `/ledger/stream` (live deltas only) open alongside `/view/stream`; stated in `docs/api.md` and in root 1065.

## Open decision

- A vs B as the end state (recommended: A; B only as the interim so the desktop migration can merge).

## Acceptance criteria

- [ ] `/view/stream` emits `text`/`text_end` for every open call of the tree (combined and split deployments); a client reconnecting mid-answer gets the text so far.
- [ ] The committed message supersedes the live text (no duplicate bubble) in a two-client check; ephemeral frames never change the view cursor.
- [ ] A test red when the hub subscription is removed from the route.
