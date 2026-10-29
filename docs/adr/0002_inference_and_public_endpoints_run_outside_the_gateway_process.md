# ADR 0002: Inference and public endpoints run outside the gateway process

Status: Accepted.
Date: 2026-10-29

## Context

Until 0.17 the gateway process did everything: accounts and sign-in, the HTTP routes and the
console, the run stores and the ledger, run ticking, and every AbstractCore provider call, with
in-process engines (llama.cpp through the Hugging Face provider, transformers, MLX) loading their
weights into the gateway's own memory. On the fresh Linux release server (2026-10-28) llama.cpp ran
out of KV slots (`decode: failed to find a memory slot`) and a Core GGUF bug followed; the
operator saw "the gateway crashed": people's sessions, the console and `/api/health` went with the
model. Root 1060 makes blast-radius isolation a standing duty; gateway 0944 asked for the
OpenAI-compatible endpoint in a separately supervised process; 0943 asks for an incident record of
every unavailability and restart.

What already existed: split mode (`serve --no-runner` + `abstractgateway runner`, opt-in) moves the
runs and the model into a second process, but that process is started by the operator, not
supervised by the gateway, and a model crash there still stops every run. Core's serving app
(`/v1`) already runs behind a loopback port with a per-boot secret in split mode
(`split_mode.ModelHost`, `forward_core_request`). Generated media already runs in subprocesses
(`media_subprocess`). Stdio MCP servers already run as child processes per call; the gateway serves
no MCP endpoint of its own (it is an MCP client).

## Decision

1. **The gateway process owns** accounts, routes, authentication, reach rules, the request log, the
   run stores and the ledger, run ticking, and supervision. In the `process` model-host mode it
   loads no model and calls no provider.
2. **The model host** is one child process the gateway starts and supervises
   (`python -m abstractgateway.model_host_child`). It holds every AbstractCore provider and the
   shared model pool: the runtime's LLM clients (the pooled client of every bundle runtime and
   the gateway-side clients: run summaries, Ask on a run, the console sandbox, entity chat) and
   Core's serving app. In the gateway each client is a proxy (`model_host.ModelHostClient`) with
   the same API as the AbstractRuntime client it replaces: every method and attribute is
   forwarded, the per-call callbacks cross the boundary (live token deltas, progress, the cancel
   event, the endpoint-profile resolver), and results and exceptions come back as the same
   objects (the runtime's retry classification sees the provider's own exception). Transport: an
   authenticated local socket (Unix socket in a 0700 folder, a Windows named pipe), a per-boot
   32-byte key handed over stdin, pickle framing between two copies of the same installed code;
   it is never reachable from the network.
3. **`/v1` is served by the model host**, behind the gateway: the gateway admits the request
   (key, Who can connect, Guest, size caps, the request log) and forwards it to Core's app on the
   child's loopback port with the per-boot secret (the split-mode forwarder). `/v1` does not get
   a process of its own: inference must use the one model pool, so a separate `/v1` process would
   either load every model a second time or forward to the model host anyway (one more hop, the
   same failure domain). A `/v1` flood saturates the model host, never the gateway's routes.
4. **MCP**: the gateway serves no MCP endpoint; stdio MCP servers already run as child processes
   per call and HTTP servers are remote. Any MCP endpoint the gateway comes to serve runs as a
   child under the same supervisor and rules (backlog 0968). MCP client calls stay in the gateway,
   bounded by their call timeout.
5. **Supervision** (`model_host.ModelHostSupervisor`), the same for every child under this ADR:
   readiness line within 60 s; a ping every 5 s, three missed pings (5 s each) = unresponsive,
   killed; restart with backoff 0.5, 1, 2, 4, 8, 16, 30 s; at most 5 restarts in 10 minutes, then
   it stops restarting (`failed`, shown in `/api/health`) until the gateway restarts; a memory
   ceiling on the child's resident memory (default 80 % of physical memory, `--model-host-memory-gb`;
   over it the child is killed); the child runs at `nice +5` (`--model-host-nice`) so the gateway
   answers first when inference saturates the CPUs; the child exits when the gateway's end of its
   stdin closes (the gateway died), so it never outlives it.
6. **Every unexpected exit is an incident** (0943's record, minimal): one file
   `<data dir>/incidents/model-host-<stamp>-<kind>.json`, schema `abstractgateway.incident.v1`,
   kind `exit`, `memory_ceiling`, `unresponsive` or `gave_up`, with the exit code or signal, the
   resident memory last sampled, the requests in flight (method, provider, model), the restart
   decision and the tail of the child's log (`<data dir>/logs/model-host.log`). `/api/health`
   carries the newest one.
7. **Failures are request failures.** A provider exception (KV full, a GGUF error) fails that
   request with the provider's own error. The child dying during a request fails it within a
   second with a sentence naming the exit ("The model host stopped while answering (killed by
   signal 9) ...; send the message again in a few seconds"), status 424, not retried
   automatically: the request may be what crashed it, and a retry would feed a crash loop. A
   request while the child is restarting fails at once with status 503 (retryable).
8. **Modes.** `serve --model-host process|in-process` (also on `runner`). Default: `process` on
   Linux (servers), `in-process` on macOS and Windows (the menu-bar tray, Apple unified memory)
   and with `--reload`. Embedding the app without the CLI (tests, `create_app`) is `in-process`.
   The gateway never falls back from `process` to `in-process` silently: a child that cannot
   start leaves inference unavailable with that reason in `/api/health`.
9. **Latency budget (root 1060).** The hop may add at most 2 ms p50 to a chat step on localhost,
   measured with `scripts/bench_model_host_hop.py` (in-process versus through the model host, the
   same provider) and recorded in the release report.

## Consequences

### Positive
- A provider crash, a native abort, a memory blow-up or a wedged engine costs the requests in
  flight, never the gateway: sessions, the console, sign-in and `/api/health` keep answering.
- Every crash leaves a record with its cause; restarts are bounded.
- One model pool for runs and `/v1`, as before.

### Negative
- One more process (the Python interpreter and AbstractCore's imports, about 200-400 MB).
- A restart loses what the child held in memory: resident models (warmed again at once), prompt
  caches, residency locks.
- The proxy depends on the AbstractRuntime client's attribute names (`_handlers`,
  `_abstractcore_llm_client`, `_chat_summarizer`) to install itself in a runtime built by
  `create_local_runtime`; `tests/test_model_host_runtime_install.py` fails when they move.
  Runtime backlog: a `create_local_runtime(llm_client=...)` parameter retires this.

### Neutral
- Still in the gateway process in this stage: speech routes that call the voice facade directly
  (console voice preview, dictation), the embeddings manager, providers that tools build through
  the session-route path. Backlog 0968 moves them.

## Enforcement
- `tests/test_model_host_*.py`: the proxy's parity (results, exceptions, live deltas, cancel,
  callbacks), the supervisor (restart, backoff, give-up, incident files, memory ceiling), the
  runtime installation, and `/v1` forwarding in `process` mode.
- `tests/test_model_host_kill_e2e.py` (opt-in, `ABSTRACT_E2E_MODEL_HOST=1`): a real `serve
  --model-host process`, a chat killed with `kill -9` on the child: `/api/health` stays 200, the
  run fails with the sentence within 5 s, an incident file exists, the child restarts, the next
  run completes.
- Review refuses a provider call or a model load added to gateway code outside the clients the
  model host proxies.

## Validation
- The kill test numbers and the hop latency in the release report. First numbers (2026-10-29, PR
  #28): `kill -9` on the child during a run: the run failed 0.06 s (macOS) / 0.10 s (Linux) after
  the kill with the sentence, `/api/health` answered 200 throughout, the incident file existed, the
  child was back after 0.62 s / 0.76 s and the next run completed. Hop: plain chat step +0.10 ms
  p50 (macOS) / +0.52 ms p50 (Linux, 4 vCPU), streamed with live deltas within noise; budget 2 ms.

## Backlog links
- 0944 (endpoint as a supervised process: `/v1` through the model host), 0943 (incident ledger:
  first non-watchdog writer), 0967 (stage 1, this ADR's implementation), 0968 (stage 2: the remaining in-process engines, MCP), root 1060.

## Related
- ADR 0001 (Core through Runtime facades: the child uses the runtime's clients, never Core).
- `docs/configuration.md` (Model host), `docs/architecture.md` (Deployment shapes).
