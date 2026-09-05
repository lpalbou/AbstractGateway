# Proposed: A DENY verb for run tool policy (default stance + exceptions)

## Metadata
- Created: 2026-08-29
- Status: Proposed
- Completed: N/A
- Roadmap phase: 2 (coherence)
- Effort: M

## ADR status
- Governing ADRs: None found for tool-approval policy shape. If this is
  adopted, the default-stance semantics (what an unlisted tool does) is
  durable cross-client policy and should become an ADR before closure —
  every thin client will encode it.

## Context

Thin clients want to offer their operators a granular, legible tool
permission model. The shape asked for (AbstractCode operator,
2026-08-28) is a **default stance plus an exception list**:

1. approve all
2. deny all
3. deny all + whitelist some
4. approve all + blacklist some

Modes 1 and 2 are just 3 and 4 with an empty exception set, so the real
model is two fields: a default (`allow` | `deny` | `ask`) and a set of
named exceptions that invert it.

Today the run-scoped vocabulary cannot express three of the four. It has
two lists, and **both are allow-shaped**.

## Current code reality (verified)

- The run policy the client sends is
  `_runtime.tool_policy = {"auto_approve_tools": [...],
  "require_approval_tools": [...]}` —
  `abstractruntime/src/abstractruntime/integrations/abstractcore/effect_handlers.py:2731`,
  parsed at `:2746-2750` and applied as `ToolApprovalPolicy(...)` at
  `:2815`.
- The enforcement rule is documented at
  `abstractruntime/src/abstractruntime/integrations/abstractcore/tool_executor.py:950`:
  *"Any tool name not in `auto_approve_tools` requires approval."* So
  the axis is **auto-approve vs. ask-a-human**. There is no third
  outcome.
- `require_approval_tools` therefore means ASK, never REFUSE. A tool
  listed there still runs once a human (or an unattended client policy)
  answers the wait.
- Grepping `abstractruntime/src` and `abstractgateway/src` for a
  deny-shaped run-policy key (`deny_tools`, `denied_tools`,
  `blocked_tools`, `tool_denylist`) returns nothing. The only denylists
  in the tree are unrelated: a security name-denylist in
  `abstractruntime/src/abstractruntime/identity/tools.py:1063,1430` and
  `workspace_ignored_paths` for filesystem scoping.
- A separate and *different* axis already exists and is well-built:
  `GET /tool-grants` (`abstractgateway/src/abstractgateway/tool_grants.py:48-79`)
  publishes a four-tier consent vocabulary — `observe(1)`, `act(2)`,
  `outreach(3)`, `destroy(4)` — with human-written teaching lines, a
  default grant with attribution, and an append-only grants ledger.
  That is *risk banding*; this item is *per-tool stance*. They compose
  (a band can seed the exception set) and neither replaces the other.

**Consequence:** a client asked for "deny all + whitelist" can only
approximate it by putting every tool in `require_approval_tools` and
then denying each prompt by hand. Unattended runs stall instead of being
refused, and the operator's stated intent ("never run this") is not
carried on the wire at all — it lives only in the client's UI, where the
run cannot see it.

## Scope

- Add a deny stance to the run-scoped tool policy that the RUNTIME
  enforces, so a denied call is refused without a human round trip and
  without the tool executing.
- Express the default stance explicitly rather than by omission, so
  "what happens to a tool nobody named" is a stated fact rather than an
  implicit `ask`.
- Keep the existing two-list form working unchanged (clients in the
  field send it today).

## Non-goals

- Not a replacement for `/tool-grants` risk tiers — that axis stays.
- Not per-argument or per-path policy (no "deny `rm` but allow `rm` in
  `/tmp`"). Name-level only; argument-level policy is a separate,
  harder item.
- Not a client-side feature. Client UI is
  `abstractcode-tui/docs/backlog/proposed/0001_granular_tool_permission_model.md`,
  which is BLOCKED on this.

## Sketch (for discussion, not a decided contract)

```
_runtime.tool_policy = {
  "default": "ask" | "allow" | "deny",   # explicit; absent = "ask" (today's behavior)
  "auto_approve_tools": [...],           # unchanged
  "require_approval_tools": [...],       # unchanged, still means ASK
  "deny_tools": [...]                    # NEW: refuse without asking
}
```

Open questions that need a decision before implementation:

- **Precedence.** If a name appears in more than one list, which wins?
  Proposal: `deny` > `require_approval` > `auto_approve`, i.e. the
  safest stance wins, and a contradiction is a client bug that fails
  safe rather than silently picking the permissive branch.
- **What the model sees.** A denied call must come back as a tool
  result the loop can reason about ("denied by policy: <tool>"), not as
  a silent no-op or a crash — otherwise the agent retries forever. This
  is the same lesson as the deny-with-a-reason work: a refusal the
  model cannot read teaches it nothing.
- **Does `default: "deny"` mean the run cannot start?** A workflow whose
  every tool is denied is a run that can only talk. That may be a
  legitimate "read-only conversation" mode, or it may deserve a refusal
  at start. Decide deliberately.
- **Whose default?** `/tool-grants` already has a gateway-level default
  grant with attribution. If a run sends no `default`, does the
  gateway's grant supply one, or does absence stay `ask`? Layering
  server default under run override is the likely answer, but it must
  be stated, not inferred.

## Expected outcomes

- A run can be started that refuses named tools without a human, and the
  refusal is visible in the ledger as a policy decision.
- A thin client can render the operator's four modes truthfully, because
  each one is expressible on the wire.
- Absent fields behave exactly as today (no migration for existing
  clients).

## Validation

- A run with `deny_tools: ["execute_command"]` where the model calls it:
  the call does not execute, the ledger carries a policy-refusal record,
  the loop receives a readable result, and no approval wait is created.
- Precedence: a name in both `deny_tools` and `auto_approve_tools` is
  denied, and the contradiction is reported rather than silently
  resolved.
- `default: "deny"` with a whitelist: only whitelisted tools execute;
  every other call is refused without a wait.
- Back-compat: a policy carrying only the two existing lists produces
  byte-identical behavior to today (pin it, so this cannot regress the
  clients already in the field).
- Unattended: a denied call must not create a wait that an unattended
  driver then has to answer — that is the stall this item exists to end.

## Related

- `abstractgateway/src/abstractgateway/tool_grants.py` — the risk-tier
  axis this composes with.
- `abstractcode-tui/docs/backlog/proposed/0001_granular_tool_permission_model.md`
  — the client half, blocked on this.
- `abstractcode-tui/docs/design/thin-client-conformance.md` open
  violation #6 (*"gateway should serve `auto_approve_at` directly"*) —
  the same surface, adjacent question.
