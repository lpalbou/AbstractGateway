# 0978 — One weights copy per model in the model host, and the model queue visible on the Models row, `/api/health` and `/v1` (Core ADR 0011)

**Status**: planned · **Priority**: P0 (this wave, the gateway half) · **Created**: 2026-10-29 (architect of the shared-weights P0)
> Root link: AbstractFramework 1064 (concurrency benchmark), recurrent 1060 (resilience standing duty). Master: Core ADR 0011 (`docs/adr/0011-shared-weights-and-concurrent-requests.md`), gateway ADR 0002 (the model host child owns the scheduler in `process` mode), Core 0870, runtime 0866.

## Why
The 2026-10-29 audit (`untracked/residency-probe/`, `probe_gateway.py`): through the model host child a GGUF model
was built once by the pool warm-up (12.15 GB) and again by the first console-sandbox call (24.21 GB); after `kill -9`
and the warm rebuild, twice again. On the Linux release server's 16 GB card that is the `failed to find a memory slot`
of 2026-10-28. Core ADR 0011 decides one weights object per model for every path and a per-model slot scheduler, with a
queued state that is never silent; this item is the gateway's half.

## In scope (this wave)
1. **R2 (worker D)**: `_gateway_llm_client` (`routes/gateway.py`) and `model_host.local_client` stop building a
   `LocalAbstractCoreLLMClient` per gateway-side generation; they borrow the runtime 0866 lease on the principal's
   pool (in the child in `process` mode, in this process in `in-process` mode). The gateway adds no lock or worker.
2. **Restart rebuilds one copy**: the replayed `set_*` configuration after a child restart warms each model once
   (the audit's two builds after restart is a defect); the warm-up and the first request on the same model share
   the load (the Core registry serialises two loads of one key).
3. **The queued state surfaces**: the Models row and `/api/health` carry Core's pool fields (`slots`, `slots_max`,
   `slot_bytes`, `active`, `queued`, `slot_note`) relayed by the runtime; the console shows "2 active, 3 queued,
   2 of 4 slots (a 3rd slot needs 2.1 GB, 0.6 GB free)" and offers "Set concurrency" (runtime 0866 facade, exact or
   refused in words, next to "Set context"); the chat status shows "Queued behind N requests on MODEL" from the
   `queued` ledger event (runtime 0866); the typed `model_queue_full` / `model_queue_timeout` render their
   sentence. `/v1` forwards Core's `X-AbstractCore-Queued` header and the SSE comment lines untouched.
4. **Tests** (red without the fix):
   - `tests/test_model_host_one_weights_copy.py`: the audit probe as a test (ADR 0011 Enforcement a): a real
     `serve --model-host process` with the fake engines of `model_host_fakes.py` counting builds; admin pool, bob
     pool, `local_client` (summary, sandbox), `/v1/chat/completions`, `/v1/embeddings`, the gateway embeddings
     route, then `kill -9` of the child: `builds == 1` per child generation and per model, residency `holders` ==
     number of paths, one `weights_id`. Real-weights twin (opt-in `ABSTRACT_E2E_MODEL_HOST=1`, Qwen3.5-4B-GGUF
     and Qwen1.5-0.5B-4bit) records RSS per step like the probe did.
   - `tests/test_models_row_queue_state.py`: the pool fields on the row and `/api/health`; the console sentence;
     "Set concurrency" through the facade; refusal in words.
   - `tests/test_v1_queue_header_and_sse_comments.py`: the header and the comment lines pass through the forwarder.
   - `tests/concurrency/` (root 1064): the scenario harness gains the chat-status assertion that a queued turn shows
     "Queued behind N" and that cancelling one queued session leaves the others' positions decreasing by one.
5. **Benchmark** (ADR 0011 Enforcement f): `scripts/perf/bench_concurrency.py` gains `--real-model provider/model`:
   matrix 1×1, 1×4, 4×1, 4×4 on one GGUF and one MLX model; per request p50/p95/p99 latency, TTFT, time queued,
   slots active, tokens/s, RSS and GPU memory min/max, weights copies (must be 1); JSON with commit ids; the first
   baseline in the release report; gate "copies == 1 and p95 queued at 4×4 ≤ 3 × single-request time".

## Out of scope
Continuous batching (Core 0871); per-principal fairness (ADR 0011 §2.3, decided from the 1064 baseline).
