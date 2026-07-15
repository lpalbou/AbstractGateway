# 0087: Resolve skills/MCP selections at generic run start

> Package: abstractgateway
> Type: feature
> Created: 2026-07-15
> Priority: P2
> Labels: seat-gateway, skills, launch-surface

## Summary

Flow ruled (c2254, answering observer's c2233 ask 4) that launch surfaces
select skills/MCP via the RUN-LEVEL lane: `start_run input_data`, terminating
on the existing `_runtime` keys (agent's c2235: `_runtime.skills_block`, the
documented loop contract). The gateway already performs names→resolved-block
resolution for the WORKFORCE lane at queue-payload build
(`skills_union.resolve_backlog_skills` via abstractskill's
`select_skills_for_context`, decision:skills-union-spawn-wiring) — but the
generic `/runs/start` lane has no resolution step yet: an
`input_data.skills` list from observer's Launch picker would ride in as
inert data.

## Direction

- At run start (gateway side), when `input_data` carries a skills selection,
  resolve names through the SAME `select_skills_for_context` path the
  workforce lane uses (one gate, never a second resolver) and write the
  resolved block to the ruled `_runtime` terminus; record
  requested/active/verdicts like the queue payload does (default-REQUESTED,
  never trust-bypassed — held/blocked ride as labeled verdicts).
- MCP half lands when the MCP capability lane has execution (the declared
  registry `/mcp/servers` is inventory-only v1; grants ride the phases
  config family per decision:workforce-capabilities-homes).
- Precedence note from flow's ruling: graph pins (later, additive) beat
  run-level defaults for their node — same class as tools_allowlist.

## Acceptance criteria

- [x] `start_run` with a skills selection produces a run whose loop sees the
      resolved skills block (test: names → `_runtime` terminus, verdicts
      recorded; blocked skill never rides)
- [x] Observer's Launch picker works end-to-end against a live run
      (gateway+runtime halves verified end-to-end through the real host:
      Agent-node child run carries the block verbatim + `read_skill`
      reachable; observer's picker unhold gates on this receipt, c2429/c2435)
- [x] Docs: api.md run-start section names the field + the trust semantics

## Completion (2026-07-15)

- Gateway half: `capability_inventories.resolve_run_skills` (the SAME
  abstractskill trust gate as `/skills` + workforce spawn — one gate),
  wired in `bundle_host.start_run`: `input_data.skills` →
  `_runtime.skills_block` (byte-stable) + `_runtime.skills_resolution`
  (requested/active/verdicts/resolved_tree_hashes) + `read_skill` appended
  to caller allowlists; caller-set blocks never overwritten. `read_skill`
  executes against the shelf with a trust RE-CHECK at read time.
- Runtime half (c2429, co-verified by agent c2435): visualflow compiler
  Agent-node child vars inherit the parent block verbatim (setdefault,
  whitespace-only never rides); `read_skill` joins EXPLICIT child
  allowlists only (empty = registry defaults stays untouched — appending
  would restrict the child to one tool).
- End-to-end pin: `tests/test_gateway_run_start_skills.py::
  test_skills_block_reaches_the_agent_node_child_run_end_to_end` (start →
  Agent spawn → child vars, both halves through the real host). Suite 770
  green.

## Receipts

- Ruling: flow c2254 (transport), agent c2235 (terminus), gateway c2243
  (inventory endpoints); operator directive 2026-07-15 16:22 via observer
  c2233
- Ship: gateway half c2286; runtime half c2429 (5 pins, 1294 green); agent
  co-verify c2435; end-to-end receipt this card's completion note
