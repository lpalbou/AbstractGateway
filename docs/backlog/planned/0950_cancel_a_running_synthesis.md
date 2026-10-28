# 0950-gateway: [IMPROVEMENT] Cancel a running synthesis

> Package: abstractgateway (route); abstractruntime (voice facade); abstractvoice (engines)
> Type: improvement
> Status: planned (not implemented)
> Created: 2026-10-28
> Priority: P2
> Labels: voice, tts, cancellation, latency
> Root link: AbstractFramework backlog 1060 (security, resilience and latency standing duty); related 0233 (real run cancellation), 0949

## Summary

A one-shot `POST /api/gateway/runs/{run_id}/voice/tts` cannot be cancelled once started. Give a running
synthesis a cancel token from the gateway route, through the runtime's voice facade, to the AbstractVoice
engines, checked between sentences; the route cancels it when the client leaves.

## Why

The console's capability editor (0949) plays a sample when a voice is chosen. A user who goes from voice to
voice abandons samples: the browser aborts its request, but the gateway keeps synthesizing each one to the end
on a local single-worker engine, and the sample the user stopped on waits behind them. The kit's 200 ms debounce
reduces this; it cannot cancel a synthesis already started.

## Current code reality

- `routes/gateway.py` `voice_tts` (~l.12754) takes no `Request`, never checks `request.is_disconnected()`, and
  starts a durable child execution; only the watchdog deadline (`timeout_s`, `_voice_tts_timeout_fail_loud`)
  ends a wedged one.
- `voice_tts_stream` (~l.12951) already frees the engine between sentences when the client leaves (tests
  `test_r13_gateway_concurrency.py`).
- The run kill switch (`stop_kill_switch.py`) covers `llm_call` effects, not a voice synthesis.

## Scope

- In: a cancel token (an `threading.Event`-like handle keyed by `request_id`) created by the route and passed
  through the runtime voice facade into AbstractVoice's synthesis call; AbstractVoice engines check it between
  sentences/batches (`split_text_batches` loops in the adapters) and stop with a typed `SynthesisCancelled`;
  the route watches `request.is_disconnected()` (as the stream route does) and sets the token; an explicit
  `POST /api/gateway/runs/{run_id}/voice/tts/{request_id}/cancel` for clients that keep the socket.
- The child execution ends `cancelled` (not failed), its partial artifact is not stored.
- Out: cancelling inside one sentence's generation (engine-internal); STT.

## Dependencies

AbstractVoice: a `cancel` parameter on the synthesis entry points (new backlog item there); AbstractRuntime: the
voice facade forwards it.

## Validation

- A fake engine that synthesizes sentence by sentence: a client that disconnects after the first sentence
  leaves the engine free within one sentence; the child run reads `cancelled`; no artifact.
- The explicit cancel route answers 202 and the same terminal state; an unknown `request_id` answers 404.
- Red check: without the disconnect watch, the engine stays busy to the end.
