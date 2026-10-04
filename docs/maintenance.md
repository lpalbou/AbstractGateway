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

Notification helpers used by `triage-reports --notify`:
- Telegram: `ABSTRACT_BACKLOG_TELEGRAM_CHAT_ID` or `ABSTRACT_TRIAGE_TELEGRAM_CHAT_ID`
- Email: sent to the administrator's registered address through the administrator's own email account
  (Accounts → **Email** on the administrator's row), from the durable notification outbox — the recipient policy and send limits apply and
  a retry never sends a notice twice. See [email.md](./email.md).

Evidence: CLI wiring in `src/abstractgateway/cli.py`.

## Backlog browsing/editing

The gateway also exposes endpoints that read/write backlog Markdown files in a folder that contains `docs/backlog/*`.

They work out of the box: without a setting the gateway uses its own folder, `<data dir>/backlog/`, created with a starter overview and item template on first use. To point it at a project checkout:

```bash
abstractgateway config set triage_repo_root /path/to/your/repo   # saved; or Continuum Settings, or the console
abstractgateway serve --backlog-root /path/to/your/repo          # this run only
```

See [configuration.md](configuration.md#backlog-folder-exec-runner-and-process-manager-continuum) for the resolution order and the three doors.

Evidence: `resolve_backlog_root` in `src/abstractgateway/runtime_config.py`, used by `src/abstractgateway/routes/gateway.py` (process manager + backlog endpoints) and `src/abstractgateway/maintenance/backlog_exec_runner.py`.

## Backlog execution runner (high risk; disabled by default)

The backlog exec runner consumes queued execution requests under `<DATA_DIR>/backlog_exec_queue/` and executes them (optionally using the `codex` CLI).

Enable (applies at once on a running gateway):

```bash
abstractgateway config set backlog_exec_runner on
abstractgateway config set executor codex        # codex | claude | cursor-agent | abstractcode
```

or `abstractgateway serve --exec-runner on` for one run, or Continuum → Settings → Gateway administration.

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

Notes:
- **Process control** (`/api/gateway/processes`, start/stop, log tail) is **repo-root scoped** for safety and assumes a monorepo-style checkout (scripts like `./build.sh`, `./agw-uat.sh`, …).
- **Env-var management** (`/api/gateway/processes/env`) is **repo-root independent** and works in packaged installs (it persists under `ABSTRACTGATEWAY_DATA_DIR`).

Enable:

```bash
abstractgateway config set process_manager on

# Process control only: the AbstractFramework checkout it manages
abstractgateway config set triage_repo_root "$PWD"
```

Process control stays off while the backlog folder is the gateway's own default folder (it is not a checkout).

Optional config path:

```bash
export ABSTRACTGATEWAY_PROCESS_MANAGER_CONFIG="$PWD/runtime/gateway/processes.json"
```

Endpoints:
- `GET /api/gateway/processes` (requires the backlog folder set to a checkout: `triage_repo_root`)
- `POST /api/gateway/processes/{id}/start|stop|restart|redeploy`
- `GET /api/gateway/processes/{id}/logs/tail`
- `GET /api/gateway/processes/env` (metadata only; never returns values; does not require repo root)
- `POST /api/gateway/processes/env` (write-only set/unset for allowlisted keys; does not require repo root)

Evidence: `src/abstractgateway/routes/gateway.py` (endpoint guards) and `src/abstractgateway/maintenance/process_manager.py`.

### Env var allowlist (write-only)

Env var editing is allowlist-only and values are write-only (they are never returned to the client). Overrides are persisted on the gateway host under:
- `<ABSTRACTGATEWAY_DATA_DIR>/process_manager/env_overrides.json`

When the gateway starts with the process manager on (`process_manager` setting), it loads and applies persisted overrides to its own `os.environ` (best-effort).

To extend the allowlist, update:
- `src/abstractgateway/maintenance/process_manager.py` → `managed_env_var_allowlist()`

## File + attachment helpers (thin-client support)

The gateway exposes helpers used by thin clients and workflows:
- Workspace policy: `GET /api/gateway/workspace/policy`
- File access: `GET /api/gateway/files/search|read|skim`
- Attachments: `POST /api/gateway/attachments/ingest` and `POST /api/gateway/attachments/upload`

Workspace folders are a setting the admin changes at any time (no restart):
the gateway policy (`GET`/`PUT /api/gateway/workspace/policy`: shared
workspace, allowed folders, allow any folder, never allowed, launch-folder
trust) and each account's switched-on folders (`/workspace/policy/{account}`).
A run started without `workspace_root` works in a folder the gateway makes for
its conversation under `<ABSTRACTGATEWAY_DATA_DIR>/workspaces/`; its file tools
reach that folder plus the account's effective folders
(`GET /api/gateway/workspace/effective/me`). The `/files/*` helpers (admin) use
the shared workspace as their root and the admin's switched-on folders as
mounts; a client may only narrow them. See
[security.md](./security.md#workspace-folders-the-admin-allows-the-account-fine-tunes).

Evidence: `src/abstractgateway/workspace_policy.py`, `_files_scope()` in `src/abstractgateway/routes/gateway.py`, tests in `tests/test_gateway_workspace_policy_r9.py`.

## Telegram bridge

Background bridges can ingest external messages and start durable runs (thin-client semantics), and may also emit events for specialized workflows.

Enable (Telegram):
- `ABSTRACT_TELEGRAM_BRIDGE=1`
- transport + credentials depend on configuration (see `src/abstractgateway/integrations/telegram_bridge.py`):
  - Bot API (default when token is present): `ABSTRACT_TELEGRAM_BOT_TOKEN=...`
  - TDLib (E2EE): `ABSTRACT_TELEGRAM_TRANSPORT=tdlib` + TDLib setup
- access control (fail-closed defaults):
  - DMs default to allowlist: set `ABSTRACT_TELEGRAM_ALLOWED_USERS=...` (numeric Telegram user_id; discover via `/whoami`)
  - Groups default to disabled (opt-in via `ABSTRACT_TELEGRAM_GROUP_POLICY=allowlist|open`)
- Optional: override which workflow to run per message:
  - `ABSTRACT_TELEGRAM_BUNDLE_ID=...`
  - `ABSTRACT_TELEGRAM_FLOW_ID=...`
  - Default (when unset): shipped `basic-agent` bundle entrypoint.
- Tool approvals:
  - `ABSTRACTGATEWAY_TOOL_MODE=approval` (default): safe tools run in-process; dangerous/unknown tools require `/approve` or `/deny`.
  - `ABSTRACTGATEWAY_TOOL_MODE=passthrough`: approval required for *all* tools (including safe ones); after approval, the runtime executes the tool batch in-process.
  - `ABSTRACTGATEWAY_TOOL_MODE=delegated`: tools are not executed locally; workflows enter a durable `JOB` wait for external executors.
- optional knobs:
  - Telegram-only routing override: `ABSTRACT_TELEGRAM_MODEL` (and optionally `ABSTRACT_TELEGRAM_PROVIDER`)
  - Replayed history is the gateway's window (the most recent 50,000 tokens of whole turns); `ABSTRACT_TELEGRAM_MAX_HISTORY_MESSAGES` is retired and ignored (a warning is logged when it is set)
  - `/reset` controls: `ABSTRACT_TELEGRAM_RESET_DELETE_MESSAGES`, `ABSTRACT_TELEGRAM_RESET_DELETE_MAX`, `ABSTRACT_TELEGRAM_RESET_MESSAGE`

Evidence: bridge startup in `src/abstractgateway/service.py` (`start_gateway_runner`).

## Email

Email is configured per user (Accounts → **Email** on your own row), never through environment variables; new mail reaches
automations through the `email.received@1` trigger. See [email.md](./email.md) for the watcher, notifications,
recovery codes and the one-time import of the retired `ABSTRACT_EMAIL_*` variables, and [api.md](./api.md#email) for
the routes (including the deprecated `/api/gateway/email/*` aliases).

## Related docs

- API overview (core client contract): [api.md](./api.md)
- Security: [security.md](./security.md)
- FAQ: [faq.md](./faq.md)
