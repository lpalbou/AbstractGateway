# Proposed: Tenant execution isolation tiers (the multi-user way forward)

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 1 (make hosted multi-user safe)
- Effort: S (tier 1) + M (tier 2) + L (tier 3)

## ADR status
- Governing ADRs: None. This should produce an ADR defining the hosted
  multi-user trust model ("what isolation level each deployment mode guarantees").
- Depends on 0084 (per-user RBAC and workspace grants) as its policy ceiling.

## Context

Multi-user hosting is a recent, wanted capability. The intended model is
`1 user = 1 runtime`, each user isolated from the others. This item defines a
truthful, incremental way to deliver that safely rather than dropping the
feature.

## Current code reality (investigated)

The data-plane isolation is real and tested:

- `src/abstractgateway/service.py:98-99`: multi-user mode is on whenever
  `ABSTRACTGATEWAY_USER_AUTH=1`.
- `service.py:147-162` creates a separate `GatewayService` per principal; each
  gets a physically separate data dir
  (`service.py:130-131`: `.../users/<tenant>/<runtime_id>/runtime`), separate
  run/ledger/artifact stores, a separate runner thread, and a separate bundle
  host.
- `tests/test_gateway_principal_isolation_matrix.py` proves the API boundary:
  Bob receives `404` on Alice's runs, ledgers, history, input_data, artifacts,
  bundles, VisualFlows, session artifacts, capability defaults, and KG memory.

The gap is OS-level, and it is conditional and narrow:

- When Gateway and Runtime are co-located, all per-principal runtimes are
  co-resident in ONE OS process and UID. The isolation is logical (separate
  Python objects + directories), not enforced by an OS boundary.
- Tools execute in the Runtime (`hosts/bundle_host.py` ~960-1000).
  AbstractRuntime's own `integrations/abstractcore/workspace_scoped_tools.py:16`
  states plainly: "`execute_command` is not a sandbox; commands can still write
  outside via absolute paths / `cd ..`."
- The concrete default tool map (`abstractruntime/.../abstractcore/default_tools.py`
  `get_default_toolsets()`) is:
  - `files`: `list_files`, `skim_folders`, `search_files`, `analyze_code`,
    `skim_files`, `read_file`, `write_file`, `edit_file` (+ runtime-owned
    `open_attachment`). These take path ARGUMENTS, which `workspace_scoped_tools`
    rewrites/clamps — under the default `workspace_only` mode an absolute path
    outside `workspace_root` is rejected, so these do NOT cross tenants.
  - `web`: `skim_websearch`, `skim_url`, `web_search`, `fetch_url`. Network
    egress, not cross-tenant disk reads (but see SSRF note below).
  - `system`: `execute_command` — the ONLY default tool that runs an arbitrary
    shell string. Path-argument clamping cannot confine a shell command, so this
    is the single tool that breaks tenant isolation in-process.
  - `comms` (opt-in via `ABSTRACT_ENABLE_*` env): email/WhatsApp/Telegram
    send/read. Off by default.
- Two things un-clamp the otherwise-safe file tools: selecting an escaping
  `workspace_access_mode` (`all_except_ignored` or `workspace_or_allowed`) lets
  `read_file`/`write_file`/`edit_file` reach absolute paths outside the
  workspace; a run can request these modes today (see AGENTS.md
  2026-02-22 notes), so a hosted regular user must not be able to WIDEN beyond
  their granted workspaces.
- Default `tool_mode=approval` (`bundle_host.py:978`) gates dangerous tools
  behind an approval wait — but in a self-service flow the same user supplies the
  approval. So a tenant who authors a flow with `execute_command` and approves it
  runs a shell as the Runtime UID and can read `.../users/<otherTenant>/...` on
  disk, plus provider keys.

Multi-workspace allowlisting already exists (per run): `input_data` carries
`workspace_root`, `workspace_access_mode`, `workspace_allowed_paths`
(newline-separated roots, mounted), and `workspace_ignored_paths`
(`routes/gateway.py:1691-1694`, `2846-2928`). What is MISSING is a durable
per-USER RBAC: a principal-level set of granted folders with read/write/execute
permissions that acts as the CEILING for every run (per-run input can only narrow
within it, never widen). Today the principal model
(`security/principal.py`) has only coarse roles (`admin`/`user`/`readonly`) and
scopes — no per-user filesystem grants. See 0084.

Conclusion: "different runtime" today means "different logical runtime instance,"
which is sufficient for data/API isolation but NOT against a tenant who executes
host code. The correct fix is NOT to remove `execute_command` (that cripples the
agent) — it is to run it SANDBOXED for regular users, UNSANDBOXED for admins (who
own the machine), and to bound all execution by per-user RBAC workspace grants.

## Problem or opportunity

The functionality is needed; the honest requirement is to make the *execution*
boundary match the *data* boundary, tiered by the trust level of the tenants.

## Deployment reality: Gateway and Runtime are not necessarily co-located

This is central to the design. Gateway is the control plane; the Runtime that
actually executes tools may run on a different machine (split deployment). So
execution isolation is fundamentally a RUNTIME-HOST concern, not something the
Gateway process can provide by itself:

- Tier 1 (tool/mode policy) is set by Gateway per principal and PROPAGATED to the
  Runtime via run input/vars; it is enforceable regardless of co-location.
- Tiers 2-3 (OS/sandbox isolation) must be enforced where the Runtime executes
  tools. Gateway's job there is to (a) require/propagate the policy and (b) not
  advertise an isolation guarantee it cannot verify when the Runtime is remote or
  untrusted. Truthful capability reporting (see 0055) should therefore state the
  execution-isolation tier of the bound Runtime, not assume co-location.

## Design correction: sandbox `execute_command`, do not remove it; admins own the machine

`execute_command` is essential to an effective agent and must NOT be removed.
The correct model, per admin vs regular user:

- Admin principals (`is_admin()`): full ownership — `execute_command` runs
  UNSANDBOXED as a human operator would, across their granted workspaces. Admins
  are trusted to own the system.
- Regular users: `execute_command` still available, but runs SANDBOXED (the
  general good practice), confined to that user's RBAC-granted workspaces, and
  bounded by cgroup/rlimit resource caps. Escalation to unsandboxed is an
  explicit approval, not a default.

This replaces the earlier "remove execute_command for regular users" framing,
which was wrong.

## Posture inversion: default-sandboxed with explicit opt-out (2026-07-08)

Today's posture is inverted from the honest design. An admin already gets full
control by combining existing knobs: tool mode `local` (or approving the
approval-gated call), `workspace_access_mode=all_except_ignored`, and
`allow_dangerous` where a tool requires it. What does NOT exist is the inverse —
a real sandbox to confine execution. The "opt-out of confinement" is therefore
IMPLICIT today: nothing confines shell execution, so every approval is silently
an unsandboxed approval.

Runtime tool-gating (allowlists, approval waits, `workspace_scoped_tools` path
clamping) is necessary but NOT sufficient: it is a logical policy layer enforced
in-process, and a shell string cannot be path-clamped. Gating decides WHETHER a
command runs; only an OS boundary decides what a running command CAN TOUCH.
Defense-in-depth needs both.

When this item lands, the posture must flip to:

- DEFAULT: `execute_command` (and the persistent exec session of agency-parity
  0215/0220, which must inherit the same confinement) runs inside the OS
  sandbox appropriate to the tier (Landlock+seccomp / Seatbelt / container).
- OPT-OUT: unsandboxed execution is an EXPLICIT, auditable admin flag (per
  principal or per run, recorded in the ledger), never an implicit consequence
  of "no sandbox available". Regular users cannot opt out; admins can.
- The approval prompt must state the posture truthfully: "runs unsandboxed
  (no OS confinement available)" until Tier 2/3 exists, "runs sandboxed;
  admin opt-out available" after.

## What we might want to do (tiered)

- Tier 1 — safe defaults now (Effort S): in multi-user mode, for regular users,
  bound every run by the per-user RBAC workspace ceiling (0084): a run's
  `workspace_root` / `workspace_allowed_paths` / `workspace_access_mode` may only
  NARROW within the user's granted folders, never widen beyond them. Admins are
  unrestricted. Until Tier 2 sandboxing lands, regular-user `execute_command`
  either (a) runs delegated to an external executor or (b) is gated by approval,
  with the honest limitation documented that in-process shell is not OS-confined.
  This is enforceable even when Runtime is remote (policy propagates via run
  input/vars), and it makes the shipped feature safe for trusted-team hosting.
- Tier 2 — sandboxed per-runtime execution (Effort M): run regular-user tool
  execution (especially `execute_command`) under OS confinement on the Runtime
  host — either a dedicated non-human "agentic OS user" (a locked-down service
  account per runtime, with only its granted workspaces bind-mounted and nothing
  else readable) and/or a per-invocation sandbox wrapper. Admins run unsandboxed.
  This is where regular-user `execute_command` becomes safe to run in-process.
  It composes with the split topology because it lives where tools run.
- Tier 3 — untrusted multi-tenant (Effort L): container/namespace (or microVM)
  per runtime or per run on the Runtime host, with seccomp, cgroup
  CPU/mem/pids/time limits, and network-egress control (also closes the `web`
  toolset SSRF vector). Required before selling untrusted multi-tenant hosting.
  A concrete reusable reference is the codex `linux-sandbox` + `execpolicy`
  model (see 0083): a stateless per-invocation helper that applies
  Landlock+seccomp (Linux) / Seatbelt (macOS) then `execvp`, plus a prefix-rule
  allow/prompt/forbid policy with a denial→approval→unsandboxed-retry ladder.
  Evaluate reusing/mirroring it rather than inventing one; note its caveats (no
  read confinement without per-user OS users; no built-in resource limits).

## Dependency boundary

Tier 1 is Gateway-configurable (RBAC ceiling from 0084 enforced on run input,
tool mode, admin-vs-user policy, propagated to Runtime, plus a docs +
truthful-capability contract). Tiers 2-3 are AbstractRuntime tool-executor
concerns (agentic-OS-user execution, per-invocation sandbox, workspace mounting,
resource limits) enforced on the Runtime host; Gateway relays the policy and
reports the true tier. The admin-vs-user unsandboxed/sandboxed distinction is
policy the Gateway sets and the Runtime enforces.

## Why

Keeps the wanted multi-user capability, states its true guarantee per tier,
respects the non-co-located topology, and gives a concrete path from "safe for
trusted teams now" to "safe for untrusted tenants later" instead of a binary
drop-or-ship choice.

## Promotion criteria

Promote Tier 1 now (it is the safety floor for the already-shipped feature).
Promote Tier 2/3 when an untrusted-multi-tenant deployment is a real commitment.

## Validation to require on promotion

- Tier 1: tests proving that in multi-user mode a regular user's run cannot WIDEN
  its workspace beyond the per-user RBAC grant (0084) — a request for a
  `workspace_root`/`allowed_paths` outside the grant is rejected or clamped —
  while an admin is unrestricted; and that logical isolation holds under the
  default policy.
- Tier 1: truthful-capability test — the reported execution-isolation tier
  reflects the bound Runtime and does not assume co-location.
- Tier 2/3: an adversarial test where tenant A's flow attempts to read tenant B's
  data dir via `execute_command` and is denied by the OS/agentic-user/sandbox
  boundary on the Runtime host; and a test that an admin's `execute_command` runs
  unsandboxed as intended.
- Posture flip: a test that sandboxing is the DEFAULT for `execute_command` and
  the persistent exec session (agency-parity 0215/0220) once the sandbox exists;
  that unsandboxed execution requires the explicit admin opt-out flag and is
  recorded in the ledger; and that a regular user requesting the opt-out is
  refused.
- An ADR recording the per-tier trust guarantee, the admin-vs-user execution
  policy, the default-sandboxed/explicit-opt-out posture, the split-deployment
  responsibility split, and the deployment matrix.
