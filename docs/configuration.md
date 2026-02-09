# AbstractGateway — Configuration

AbstractGateway is configured primarily via **environment variables** (plus a few CLI flags).

## Install extras (recommended)

The base install (`pip install abstractgateway`) includes the runner + durable stores + CLI.

Optional extras (see `pyproject.toml`):
- `abstractgateway[http]`: FastAPI + Uvicorn (`abstractgateway serve`)
- `abstractgateway[visualflow]`: VisualFlow JSON directory mode via `abstractflow`
- `abstractgateway[telegram]`: Telegram bridge dependencies (AbstractRuntime’s AbstractCore integration)
- `abstractgateway[voice]`: voice/audio endpoints (TTS + STT) via `abstractvoice`
- `abstractgateway[all]`: batteries-included install (HTTP + tools + voice + media + visualflow)
- `abstractgateway[dev]`: local dev/test deps

Optional (required by some workflows/features):
- `abstractruntime[abstractcore]>=0.4.2`: required to execute bundle workflows that contain LLM/tool nodes (see `src/abstractgateway/hosts/bundle_host.py`) — already included by `abstractgateway[http]`
- `abstractagent`: required for Visual Agent nodes (bundle mode) — already included by `abstractgateway[http]`
- `abstractmemory[lancedb]` (or `abstractmemory` + `lancedb`): required for bundles that use `memory_kg_*` nodes

## Core environment variables

### Paths + workflow source

- `ABSTRACTGATEWAY_DATA_DIR`: durable data directory (default: `./runtime`)  
  Evidence: `src/abstractgateway/config.py` (`GatewayHostConfig.from_env`)
- `ABSTRACTGATEWAY_FLOWS_DIR`: workflows directory (default: `./flows`)  
  Evidence: `src/abstractgateway/config.py`
- `ABSTRACTGATEWAY_WORKFLOW_SOURCE`: `bundle` (default) or `visualflow`  
  Evidence: `src/abstractgateway/service.py` (`create_default_gateway_service`)

### Durability backend

- `ABSTRACTGATEWAY_STORE_BACKEND`: `file` (default) or `sqlite`  
  Evidence: `src/abstractgateway/service.py`
- `ABSTRACTGATEWAY_DB_PATH`: SQLite DB file path (optional; default: `<DATA_DIR>/gateway.sqlite3`)  
  Evidence: `src/abstractgateway/stores.py` (`build_sqlite_stores`)
  Note: for safety, when `ABSTRACTGATEWAY_STORE_BACKEND=sqlite`, the DB path must be **under** `ABSTRACTGATEWAY_DATA_DIR`.
  The gateway fails fast if `ABSTRACTGATEWAY_DB_PATH` points elsewhere (prevents cross-wiring UAT/prod durable state).

### Runner tuning (advanced)

These map to `GatewayHostConfig` and `GatewayRunnerConfig`:
- `ABSTRACTGATEWAY_RUNNER`: `1` (default) / `0` to disable runner in-process  
  Evidence: `src/abstractgateway/config.py`, `src/abstractgateway/cli.py`
- `ABSTRACTGATEWAY_POLL_S` (default `0.25`)
- `ABSTRACTGATEWAY_COMMAND_BATCH_LIMIT` (default `200`)
- `ABSTRACTGATEWAY_TICK_MAX_STEPS` (default `100`)
- `ABSTRACTGATEWAY_TICK_WORKERS` (default `4`)
- `ABSTRACTGATEWAY_RUN_SCAN_LIMIT` (default `200`)

Evidence: `src/abstractgateway/config.py`, `src/abstractgateway/runner.py`.

## LLM/tool defaults (bundle mode)

Only needed when the loaded bundle(s) contain LLM/tool/agent nodes.

- `ABSTRACTGATEWAY_PROVIDER` / `ABSTRACTGATEWAY_MODEL`  
  Used as defaults for LLM execution in bundle mode. If missing, the gateway may try to infer defaults from flow JSON; otherwise it errors.  
  Evidence: `src/abstractgateway/hosts/bundle_host.py` (`needs_llm`, `_scan_flows_for_llm_defaults`)
- `ABSTRACTGATEWAY_TOOL_MODE`:
  - `passthrough` (default): tool calls become a durable “approval required” wait
  - `local`: execute tools inside the gateway process (dev only; higher risk)
  Evidence: `src/abstractgateway/hosts/bundle_host.py` (tool executor selection)

### Embeddings (optional)

The gateway can expose an embeddings API if AbstractCore embedding deps are available.

- `ABSTRACTGATEWAY_EMBEDDING_PROVIDER` / `ABSTRACTGATEWAY_EMBEDDING_MODEL`  
  Persisted under `<DATA_DIR>/gateway_embeddings.json` for stability across restarts.  
  Evidence: `src/abstractgateway/embeddings_config.py`

## CLI flags

`abstractgateway --help` shows all subcommands (serve/runner/migrate/triage/…).

Most-used:
- `abstractgateway serve --host 127.0.0.1 --port 8080 [--no-runner] [--reload]`  
  Evidence: `src/abstractgateway/cli.py`
- `abstractgateway runner` (worker only)
- `abstractgateway migrate --from=file --to=sqlite --data-dir <DIR> --db-path <FILE>`

## Related docs

- Getting started: [getting-started.md](./getting-started.md)
- FAQ: [faq.md](./faq.md)
- Security configuration: [security.md](./security.md)
- API overview: [api.md](./api.md)
- Operator tooling env vars: [maintenance.md](./maintenance.md)
