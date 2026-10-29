### Added

- **The models run in a supervised child process** (ADR 0002, backlog 0967). `serve --model-host process`, the
  default on every OS, moves every AbstractCore provider call, the model pool and the OpenAI-compatible `/v1` into one
  child the gateway starts, pings, restarts (backoff, at most 5 restarts in 10 minutes) and kills above a memory
  ceiling (`--model-host-memory-gb`, default 80 % of the machine's memory; `--model-host-nice`, default 5). A
  llama.cpp abort, a KV cache that runs out or an out-of-memory kill costs the requests in flight, never the gateway:
  the request fails within a second with a sentence naming the exit (status 424, not retried), `/api/health` keeps
  answering and shows `model_host`, the child is back in about a second, and each crash writes
  `<data dir>/incidents/model-host-<stamp>-<id>-<kind>.json` (schema `abstractgateway.incident.v1`). The runtime's
  LLM clients in the gateway are proxies with the same API (live replies, Stop, summaries, residency and prompt
  caches included); `/v1` is admitted by the gateway (key, Who can connect, Guest) and served by the child. The hop
  adds about 0.1 ms p50 to a chat step (`scripts/bench_model_host_hop.py`, budget 2 ms). `--model-host in-process`
  keeps one process anywhere. The split `runner`
  takes the same flags. Not yet in the child: direct speech routes, the embeddings index, MCP client calls
  (backlog 0968). See [configuration.md](docs/configuration.md#model-host-the-models-run-in-a-supervised-child-process).
