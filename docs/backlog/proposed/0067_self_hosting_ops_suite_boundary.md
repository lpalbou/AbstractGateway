# Proposed: Self-hosting ops suite — trust boundary and durable execution

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 2 (identity and trust surface)
- Effort: A=S-M (trust-domain fix), B=M (router/binding/extra), C=L (ops-as-bundles)

## ADR status
- Governing ADRs: None. Should produce an ADR on "self-evolution execution
  model" (how framework self-update stays durable) and the ops trust boundary.

## Context

Gateway absorbed a self-hosting ops suite so the AbstractFramework can update its
own code durably through the runtime. The stated rationale: self-evolution must
be durable -> durable runs live in the runtime -> Gateway is the runtime's HTTP
host, so ops landed here. The ops UI currently lives in `abstractobserver`,
hidden by default, which the author considers the wrong home.

## Current code reality (investigated by two adversarial reviews)

Trust surface:

- `security/gateway_security.py` (~539-551): any valid bearer token resolves to
  `local_admin_principal` (roles `admin,user`, scopes `*`).
  `security/authorization.py:153` short-circuits on `is_admin()` before scopes
  are checked. Ops routes (`/processes/*`, `/backlog/*`, `/triage/*`) are only
  admin-gated (`authorization.py` ~77-115). So the thin-client caller and the
  operator who can execute `codex` against the repo are the same principal, on
  the same port, process, and token.
- With ops enabled, the chain is: admin token -> `POST /backlog/create` ->
  `POST /backlog/.../execute` -> `BacklogExecRunner` spawns
  `codex ... --sandbox workspace-write` with approvals "never"
  (`maintenance/backlog_exec_runner.py` ~869-870, subprocess at ~1276-1285),
  plus `POST /processes/gateway/redeploy` -> build + `os.execv`
  (`maintenance/process_manager.py` ~1063,1069). Default bind is public
  (`cli.py:226`). One auth bug is one hop from host RCE.
- Mitigations that already exist: process manager returns 404 unless
  `ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER=1` (`routes/gateway.py` ~18221-18227);
  codex is inert unless `ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER=1` +
  `ABSTRACTGATEWAY_BACKLOG_EXECUTOR=codex` (`backlog_exec_runner.py` ~878-925,
  default executor "none").

Durability premise (important correction):

- The riskiest ops paths do NOT actually use the durable command/replay engine.
  Backlog execution is a JSON file queue + a separate polling thread +
  `subprocess.run`, with the ledger written only *after* the subprocess finishes,
  best-effort, wrapped in `except Exception: pass` (`backlog_exec_runner.py`
  ~826-853). If the process dies mid-`codex`, the queue file stays "running" and
  the work is lost — post-hoc log persistence, not replay.
- The durable-run executor is an explicit TODO:
  `backlog_exec_runner.py` ~1334-1336 ("Future: execute via a bundle workflow run
  (durable)").
- `redeploy`/`restart_self` end in `os.execv`, which no ledger can replay, and
  which kills the very threads running codex jobs.

So "it must be embedded for durability" is weaker than assumed: what exists is
reuse of the runtime *stores* for observability plus a file queue, not durable
self-evolution.

## Problem or opportunity

Repo-ops (codex execution, process restart, backlog editing) are fused into the
user-facing run host, maximizing blast radius and distorting the package
identity — while not actually getting the durability that justified the fusion.

## What we might want to do (layered; A first)

- A — Break the token = admin = ops chain (Effort S-M): introduce an explicit
  `ops:exec` capability that even admins do not get from the shared/bootstrap
  token; require it on ops routes so `is_admin()` no longer implies ops. Precedent
  already in-repo: triage action links use a capability token decoupled from the
  bearer (`routes/triage.py` ~29-39). This alone removes the "one auth bug =
  RCE" property.
- B — Isolate the ops surface (Effort M): move ops routes into a dedicated
  `ops_router` mounted only when enabled, bound to loopback (or a separate port)
  by default so ops is not reachable on the public interface; gate the ops
  dependency surface behind an `[ops]` install extra with lazy imports.
- C — Make self-evolution genuinely durable (Effort L): implement the
  `workflow_bundle` executor stub so ops actions run as ordinary durable Gateway
  runs (inheriting run/ledger/replay/crash-recovery for free), with tools for
  spawn-codex, process-control, and backlog editing, and a delegated JOB worker
  (WaitReason.JOB precedent) that survives a gateway restart. Then `abstractops`
  can be a bundle pack + thin client/UI (and CLI), NOT a second runtime host —
  avoiding double-hosting, cross-process ledger writers, and split-brain.

Recommended sequencing: A (kills the acute risk cheaply, keeps code co-located),
then B (defense-in-depth + reviewability), then C (delivers the durability the
suite was supposed to have and cleanly enables an `abstractops` package/UI).
Full package extraction without C buys little; C is what makes extraction clean.

Layer C depends on 0083 (agentic orchestration parity to retire codex): the
reason codex was subprocessed is that its autonomous agency exceeded the
framework's own orchestration. C becomes truly clean only once the framework
agent can do the self-evolution work itself as a durable run — otherwise C either
still wraps codex (durable but still an external dependency) or regresses task
quality. So: 0067-A/B now for safety/identity; 0067-C together with 0083 to make
self-evolution both durable AND as capable, dropping codex entirely.

## Dependency boundary

A and B are Gateway-local. C needs the workflow-bundle executor and durable JOB
worker model; the ops logic becomes bundles+tools that the existing gateway runs,
so durability stays with the runtime and no second host is introduced.

## Why

Removes the "one hop to host RCE" property, restores the package's identity as a
run host, and — via C — finally makes framework self-evolution durable, which was
the original goal.

## Promotion criteria

Promote A now (acute security). Promote B with 0066 (router decomposition).
Promote C when durable self-evolution is prioritized and the workflow-bundle
executor is scheduled.

## Validation to require on promotion

- A: test that an admin/bootstrap token without `ops:exec` is denied on
  `/processes/*`, `/backlog/*/execute`, `/triage/*`; only an ops-capable
  principal is allowed.
- B: ops routes unreachable on the public interface by default; base install has
  no ops dependency surface.
- C: an ops action executed as a durable run survives a simulated gateway restart
  (resumes from the ledger), replacing the file-queue + post-hoc-log path.
