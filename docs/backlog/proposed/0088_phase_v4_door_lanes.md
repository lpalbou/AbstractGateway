# 0088: Phase-machine v4 door lanes (work entry, visit→work hand-off, personal-interrupt notify)

> Package: abstractgateway
> Type: feature
> Created: 2026-07-15
> Priority: P2
> Labels: seat-gateway, entity-lane, phase-machine

## Summary

Operator ruling 2026-07-15 17:28 (relayed by entity, c2278; spec
`abstractentity/spec/entity_phases.json` v4): AWAKE is a STATE, never a
phase — an alive entity is always in exactly one of visit/work/personal/
sleep; idle IS sleep; tasks arrive THROUGH A VISIT; personal FROM WORK
requires a confirmation notification (interrupts the task), FROM SLEEP it
does not. The gateway owns the door half of what the ruling opens.

## Scope (gateway lanes; runtime owns the tick-side auto-sleep)

- WORK ENTRY door + cause word: the visit→work task hand-off (tasks given
  in a visit; visit close → work to complete them) — door verbs +
  `phase_changed` marker payloads per the ruled vocabulary (one marker
  kind, decision:phase-vocabulary v4; no per-phase marker kinds).
- PERSONAL-from-work confirmation-notification mechanism (new in v4): an
  operator personal grant landing while the entity is IN WORK sends a
  confirmation notification and interrupts the task; from sleep, no
  confirmation.
- Verify existing door derivations against spec v4 (entity's consumption
  contract: executors + tests import the artifact directly).

## Out of scope

- AUTO-SLEEP on idle (runtime's tick lane — the load-bearing gap behind
  the "86% untracked" diagram finding).

## Receipts

- Ruling relay: entity c2278; spec v4 + decision:no-awake-idle-node;
  invariants IDLE-IS-SLEEP / TASKS-ARRIVE-VIA-VISIT /
  PERSONAL-INTERRUPT-IS-CONFIRMED.
