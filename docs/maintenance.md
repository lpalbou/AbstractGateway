# AbstractGateway — Operator tooling (optional)

`/api/gateway/*` includes “operator tooling” endpoints used by higher-level UIs and workflows (reports inbox, triage queue, backlog helpers, process manager, file/attachment helpers, …). These features are **not required** to use AbstractGateway as a durable run gateway.

This document groups the main non-core features and how to enable them safely.

## Safety model (read this first)

Some endpoints can:
- write files under `ABSTRACTGATEWAY_DATA_DIR`
- read files from configured workspace mounts
- start/stop local processes (process manager)
- execute queued backlog tasks (backlog exec runner)

Only enable these features on **trusted machines** and keep gateway auth enabled.  
Security enforcement for `/api/gateway/*` is in `src/abstractgateway/security/gateway_security.py`.

## Reports inbox + triage queue

Implemented in `src/abstractgateway/routes/gateway.py` and `src/abstractgateway/maintenance/*`.

Key endpoints:
- `POST /api/gateway/bugs/report`
- `POST /api/gateway/features/report`
- `GET /api/gateway/reports/bugs` / `GET /api/gateway/reports/features`
- `POST /api/gateway/triage/run`
- `GET /api/gateway/triage/decisions`

CLI helpers:
- `abstractgateway triage-reports` (scan inbox → decision queue; optional draft writing)
- `abstractgateway triage-apply <decision_id> approve|reject|defer`

Evidence: CLI wiring in `src/abstractgateway/cli.py`.

## Backlog browsing/editing (repo-dependent)

The gateway also exposes endpoints that read/write backlog Markdown files in a repository layout that includes `docs/backlog/*`.

To enable these endpoints, set the repo root:

```bash
export ABSTRACTGATEWAY_TRIAGE_REPO_ROOT="/path/to/your/repo"
```

Evidence: repo-root checks in `src/abstractgateway/routes/gateway.py` (process manager + backlog endpoints) and in `src/abstractgateway/maintenance/backlog_exec_runner.py`.

## Backlog execution runner (high risk; disabled by default)

The backlog exec runner consumes queued execution requests under `<DATA_DIR>/backlog_exec_queue/` and executes them (optionally using the `codex` CLI).

Enable:

```bash
export ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER=1
export ABSTRACTGATEWAY_BACKLOG_EXECUTOR="none"   # none|codex_cli|workflow_bundle
```

Additional knobs (see `BacklogExecRunnerConfig.from_env()`):
- `ABSTRACTGATEWAY_BACKLOG_EXEC_POLL_S`
- `ABSTRACTGATEWAY_BACKLOG_EXEC_WORKERS`
- `ABSTRACTGATEWAY_BACKLOG_CODEX_BIN`
- `ABSTRACTGATEWAY_BACKLOG_CODEX_MODEL`
- `ABSTRACTGATEWAY_BACKLOG_CODEX_REASONING_EFFORT` (`low|medium|high|xhigh`)
- `ABSTRACTGATEWAY_BACKLOG_CODEX_SANDBOX`
- `ABSTRACTGATEWAY_BACKLOG_CODEX_APPROVALS`

Evidence: `src/abstractgateway/service.py` (runner startup), `src/abstractgateway/maintenance/backlog_exec_runner.py`.

## Process manager (dev-only; disabled by default)

The process manager can start/stop a small allowlisted set of local processes and tail logs. It is intended for **trusted dev machines**.

Enable:

```bash
export ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER=1
export ABSTRACTGATEWAY_TRIAGE_REPO_ROOT="$PWD"
```

Optional config path:

```bash
export ABSTRACTGATEWAY_PROCESS_MANAGER_CONFIG="$PWD/runtime/gateway/processes.json"
```

Endpoints:
- `GET /api/gateway/processes`
- `POST /api/gateway/processes/{id}/start|stop|restart|redeploy`
- `GET /api/gateway/processes/{id}/logs/tail`

Evidence: `src/abstractgateway/routes/gateway.py` (endpoint guards) and `src/abstractgateway/maintenance/process_manager.py`.

## File + attachment helpers (thin-client support)

The gateway exposes helpers used by thin clients and workflows:
- Workspace policy: `GET /api/gateway/workspace/policy`
- File access: `GET /api/gateway/files/search|read|skim`
- Attachments: `POST /api/gateway/attachments/ingest` and `POST /api/gateway/attachments/upload`

Server-side workspace mounts (operator-controlled):

```bash
# newline-separated: name=/absolute/path
export ABSTRACTGATEWAY_WORKSPACE_MOUNTS=$'repo=/abs/path/to/repo\\ndata=/abs/path/to/data'
```

Evidence: `_workspace_mounts()` and related policy helpers in `src/abstractgateway/routes/gateway.py`.

## Bridges (Telegram, email)

Background bridges can emit events into the runtime (e.g., “telegram.message”, “email.message”) and optionally auto-start workflows.

Enable (Telegram):
- `ABSTRACT_TELEGRAM_BRIDGE=1`
- `ABSTRACT_TELEGRAM_FLOW_ID=...` (required)
- transport + credentials depend on configuration (see `src/abstractgateway/integrations/telegram_bridge.py`)

Enable (Email):
- `ABSTRACT_EMAIL_BRIDGE=1`
- IMAP credentials + polling config (see `src/abstractgateway/integrations/email_bridge.py`)

Evidence: bridge startup in `src/abstractgateway/service.py` (`start_gateway_runner`).

## Related docs

- API overview (core client contract): [api.md](./api.md)
- Security: [security.md](./security.md)
