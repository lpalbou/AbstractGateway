# 0967-gateway: Model host: providers, the model pool and /v1 in a supervised child process (ADR 0002, stage 1)

> Package: abstractgateway
> Type: reliability / security
> Status: completed (2026-10-29, Unreleased)
> Created: 2026-10-29
> Priority: P1
> Labels: resilience, isolation, model-host, openai-endpoint, incidents, latency
> Root link: root 1060 (standing duty); operator 2026-10-28: "the providers and the OpenAI and the MCP endpoint
> should run in separate processes so that it would never crash the gateway"
> Number: 0967 (0962-0966 taken on other branches)

## Summary

On the fresh Linux release server llama.cpp ran out of KV slots (`decode: failed to find a memory slot`) and
a Core GGUF bug followed: the gateway, which ran the model in its own process, stopped answering. ADR 0002
moves every AbstractCore provider call, the model pool and `/v1` into one child process the gateway starts,
watches and restarts.

## What changed

- `model_host.py`: `ModelHostSupervisor` (readiness, ping, restart backoff 0.5-30 s, at most 5 restarts in
  10 minutes, RSS ceiling, `nice`, an incident file per unexpected exit, `/api/health` `model_host`) and
  `ModelHostClient`, a proxy with the API of AbstractRuntime's `MultiLocalAbstractCoreLLMClient` /
  `LocalAbstractCoreLLMClient`: methods and attributes forwarded, live token deltas, progress, the Stop event
  and the endpoint-profile resolver bridged, provider instances kept in the child as handles, results and
  exceptions returned as themselves; `set_*` calls replayed after a restart.
- `model_host_child.py`: the child (`python -m abstractgateway.model_host_child`): the RPC listener and
  Core's serving app (`split_mode.ModelHost`), exits when the gateway's end of its stdin closes.
- Wiring: bundle runtimes (`move_runtime_client_to_host` after `create_local_runtime`), the gateway-side
  generations (`_gateway_llm_client`, entity chat), `/v1` forwarded (`split_mode.forwards_core`), the split
  `runner` starts the child before its runtimes, `serve`/`runner` flags `--model-host`,
  `--model-host-memory-gb`, `--model-host-nice` (default `process` on Linux, `in-process` elsewhere).

## Evidence

- Kill test (`tests/test_model_host_kill_e2e.py`, real `serve --model-host process`, macOS 2026-10-29): the
  run failed 0.06 s after `kill -9` on the child with the sentence; `/api/health` 200 throughout; incident
  `model-host-...-exit.json` (`signal: SIGKILL`, the request in flight); child back after 0.62 s; the next
  run completed; `/v1/models` 200 through the child before and after.
- Hop (`scripts/bench_model_host_hop.py`, n=300, macOS M-series): plain chat step +0.10 ms p50 (+0.12 p95),
  streamed with live deltas +0.02 ms p50; budget 2 ms.
- `tests/test_model_host_rpc.py` (parity, Stop, kill, restart, 503 while restarting, give-up, memory
  ceiling, unresponsive via SIGSTOP, child leaves with the gateway), `tests/test_model_host_runtime_install.py`.

## Follow-ups

- 0968 (stage 2): the remaining in-process engines and MCP.
- Runtime: a `create_local_runtime(llm_client=...)` parameter, so the gateway stops reading the runtime's
  private attributes to install the proxy.
