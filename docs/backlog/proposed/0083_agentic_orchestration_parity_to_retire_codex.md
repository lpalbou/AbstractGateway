# Proposed: Agentic orchestration parity to retire codex for self-evolution

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 2/3 (enabler for 0067 layer C — durable self-evolution)
- Effort: L (cross-package: AbstractRuntime + AbstractAgent primary; Gateway consumes)

## ADR status
- Governing ADRs: None. Should produce an ADR: "Agentic orchestration contract
  (loop, tools, sandbox, prompt projection)" shared by AbstractRuntime/AbstractAgent.

## Context and rationale

The gateway shells out to the `codex` CLI (gpt-5.2-codex, xhigh) for self-evolution
because at the time codex's autonomous agency clearly exceeded the framework's own
tools/orchestration. The clean resolution to the ops-suite durability/identity
problem (0067 layer C: run ops as durable `.flow` bundles + tools instead of
subprocessing codex) requires the framework's own agent to be as effective as
codex. If and only if we reach that parity and can PROVE it, the codex dependency
can be dropped entirely, and self-evolution becomes durable/auditable for free by
running through the existing Runtime run/ledger machinery.

## Investigation findings (codex 0.89 fork, verified in a local `codex-rs` checkout)

Codex is a **single ReAct-style loop, not a planner/executor split**
(`core/src/codex.rs` `run_turn` ~8093; the model "finishes" simply by replying
without tool calls; persistence pressure is prompt discipline). Its effectiveness
decomposes into mechanisms that are HARNESS-side (reproducible with any decent
model + good orchestration) vs MODEL-side (co-trained, not copyable):

Harness-side (reproducible — this is the opportunity):
1. `apply_patch` — verify-before-write, context-anchored (no line numbers), 4-tier
   fuzzy matching, "no re-read" contract; ~2.9k-line standalone crate
   (`apply-patch/`). Grammar-constrained emission for codex models.
2. Sandbox-escalation ladder — run sandboxed by default WITHOUT asking; on OS
   denial, ask approval WITH the failure attached; approval reruns unsandboxed
   (`core/src/tools/orchestrator.rs`). Resolves safety-vs-autonomy at zero prompts
   for read-only work.
3. Result truncation that preserves signal — middle truncation on UTF-8
   boundaries, explicit `…N tokens truncated…` marker, total-line count, exit code
   + wall time on every exec (`core/src/truncate.rs`). Aligns with our
   `#TRUNCATION` policy.
4. Stateless prompt reassembly + durable rollout — every request rebuilds context
   from structured history; everything persisted as JSONL (resumable/auditable).
   AbstractRuntime ledgers already do most of this; the gap is deliberate,
   inspectable **prompt projection** (durable history -> "what enters the next
   request"), which is what makes compaction/memory-swap safe.
5. `unified_exec` — persistent PTY sessions with partial-output yield + stdin
   write + token-capped return; escalation is a schema field with a justification.
6. Plan tool as enforced working memory — a no-op `update_plan` whose value is
   forcing externalized task decomposition, with prompt rules about plan QUALITY.
7. Parallel tool calls with a mutation lock — read-only tools run concurrently,
   mutating tools serialize (`tools/parallel.rs`).
8. Ghost git snapshots + per-turn aggregated diff (undo without touching the
   user's index).
9. `execpolicy` — a prefix-rule allow/prompt/forbid engine with shell-command
   decomposition and approval-driven self-amendment (`execpolicy/`). A strong
   upgrade path for our `_runtime.tool_policy`, orthogonal to OS sandboxing.
10. Error-as-data recovery — malformed args, bad offsets, timeouts, sandbox
    denials are all fed back to the model as tool output so it self-corrects
    instead of crashing the run.

Model-side (NOT copyable; approximate, don't replace):
- Co-trained tool surface (gpt-5.x-codex RL-trained against these exact tool
  names/grammars) and hour-long xhigh persistence. Mitigation: match the FORMATS
  the target model was trained on, and use format-retry/validator/bounded-
  correction loops (as the existing `languageGuard` work already does) to
  approximate disciplined behavior.
- Reasoning-effort depth (`xhigh`) — partly plumbing (AbstractCore's
  `model_capabilities.json` is the same idea) and partly model capability.

The fork's memory graph (SQLite typed provenance graph, `core/src/memory.rs`
~6.9k lines) already BRIDGES to AbstractCore via `/acore/blocs/kv/load` (measured
5-13x prefill gains) — so there is integration precedent, and its
"searchable != prompt-active + audited selection" design is conceptually adjacent
to AbstractMemory triples.

Thesis verdict: mostly supported. Of the mechanisms that make codex effective,
only the co-trained surface and part of reasoning depth are model-bound; the rest
is deliberate, reproducible systems engineering — and codex's own architecture
(per-model prompts, per-model tool variants, OSS-model fallbacks) shows the team
treats the harness as the product. Honest caveat: co-training is not a thin
veneer; grammar-constrained patch emission and long-horizon persistence are
trained behaviors that good orchestration approximates but does not fully replace.

## What we might want to do

Bring the framework's agent loop to functional parity on the HARNESS-side
mechanisms, then prove it on a representative self-evolution task set:

1. Adopt a verify-before-write, context-anchored patch tool (port or reimplement
   `apply_patch`; the crate is small and standalone).
2. Add the sandbox-escalation ladder + `execpolicy`-style tool policy (this also
   advances 0062 Tier 2/3 — the codex `linux-sandbox` confinement wrapper is
   stateless/per-invocation, exactly the shape a Python runtime needs; note the
   `ExternalSandbox` "I am the external sandbox" mode AbstractRuntime could claim).
3. Add signal-preserving truncation, persistent exec sessions, a plan tool with
   quality prompting, parallel read/serialized-write tool execution, and
   error-as-data tool results.
4. Make prompt projection explicit and inspectable (leverage existing ledgers).
5. Plumb reasoning effort end-to-end (main turn, subagents, compaction).
6. Build a self-evolution eval harness (representative backlog-execution tasks)
   and A/B our agent vs codex; parity is a PROVEN result on that set, not a claim.

## Dependency boundary

This is primarily AbstractRuntime (tool executor, sandbox, exec sessions, prompt
projection, effort plumbing) and AbstractAgent (loop discipline, plan/apply_patch
tools, correction loops). AbstractGateway consumes the result: 0067 layer C turns
ops actions into durable bundles+tools that this improved agent executes, at which
point the codex subprocess path is removed.

## Why

It is the clean, durable resolution to 0067: instead of a non-durable codex
subprocess, framework self-evolution runs as ordinary durable runs, auditable and
crash-recoverable via the ledger — and the same investment upgrades every agent in
the framework, not just ops.

## Promotion criteria

Promote when self-evolution parity becomes an active goal (it gates 0067 layer C).
Sequence the sandbox/execpolicy piece with 0062 Tier 2/3 since they share the
codex `linux-sandbox`/`execpolicy` reference.

## Validation to require on promotion

- A self-evolution eval set where the framework agent matches or beats codex on
  task success and durability (runs are replayable), with results recorded.
- The `apply_patch`-style tool, sandbox ladder, and truncation contract are
  covered by unit tests in the owning package.
- 0067 layer C executes a real ops action as a durable run using the framework
  agent (no codex subprocess) and survives a simulated restart.
