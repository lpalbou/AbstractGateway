# 0968-gateway: Model host stage 2: the engines still in the gateway process, and MCP (ADR 0002)

> Package: abstractgateway (+ abstractruntime facade additions)
> Type: reliability / security
> Status: planned
> Created: 2026-10-29
> Priority: P1
> Labels: resilience, isolation, model-host, voice, embeddings, mcp
> Root link: root 1060; ADR 0002; follows 0967

## Summary

Stage 1 (0967) moved every runtime LLM client and `/v1` into the model host child. Still in the gateway
process in `process` mode: (1) speech routes that call the runtime's voice facade directly (console voice
preview, dictation, the voice library), which load TTS/STT engines in process; (2) the embeddings manager
(memory and search indexes); (3) providers built by tools through the session-route path
(`attach_endpoint_profile_resolver_getter`); (4) MCP client calls (`mcp_run_tools.py`: stdio servers are already
child processes per call, HTTP servers remote; the client code itself runs in the gateway, bounded by
`CALL_TIMEOUT_S`).

## Design (decided in ADR 0002)

- Voice and embeddings: the same proxy as the LLM clients (`ModelHostClient` kinds `voice`, `embeddings`) over
  the runtime facades, so the TTS/STT and embedding weights live in the child and one pool serves all.
- Tool-built providers: the tool executor's resolver getter returns a model-host-backed constructor.
- `/v1` keeps no process of its own (it shares the model pool); the load drill 0944 asked for (0944 deprecated by ADR 0002) (a `/v1` flood and a hung
  provider) proves the console and sessions stay under the latency budget while the child saturates.
- MCP: any MCP endpoint the gateway comes to serve (runtime `mcp_worker`) is a child under the same
  supervisor; MCP client calls move to a worker child only if the load drill or an incident shows them
  hurting the gateway.
- Console: a Model host row on Resources (state, restarts, last incident, Restart button for admins);
  `abstractgateway status --incidents` (0943).

## Validation

- The kill test extended to a TTS stream and an embedding query; the 0944 load drill with p50/p95 in the
  release report; the incident files of the drill.
