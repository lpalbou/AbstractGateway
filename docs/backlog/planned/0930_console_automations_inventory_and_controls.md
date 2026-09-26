# 0930 — Console: automations inventory and controls (pause/archive), legacy distinction

**Status**: planned (phase after Automations v1) · **Priority**: P2 · **Created**: 2026-09-26
**Package**: abstractgateway · **Related**: abstractgateway 0928 (Automations v1 façade — prerequisite),
abstractframework backlog 0928 (master item)
**Design**: untracked/design/automations-PLAN.md (2026-09-26)

## Why this is here

Automations v1 ships its user surfaces in Observer and Assistant only (operator ruling
2026-09-26). The gateway console is the operator's view of everything the gateway hosts,
so once v1 lands it must show automations too — including legacy `POST /runs/schedule`
schedules, which stay unchanged and must not be presented as automations.

## This package's slice

1. **Inventory.** A console section listing automations from `GET /automations`
   (`AutomationSummary`): title, status (`active|paused|completed|archived|failed`),
   trigger, context mode, next fire time, occurrence count, last occurrence status, and
   attention (pending waits / unread).
2. **Controls.** Pause, resume and archive through `POST /automations/{id}/commands`
   (`automation.pause|automation.resume|automation.archive`), showing the `CommandReceipt`
   and then the ledger-recorded outcome (applied or rejected with its error code), never
   assuming acceptance means application.
3. **Legacy distinction.** Rows with `legacy:true` are labelled as legacy schedules and
   keep their existing controls (legacy pause/resume/`update_schedule`); no automation
   command is offered on them.
4. **Authorization.** The section uses the principal's own service; a non-admin sees only
   their automations; controls follow the same authorization as `/automations`.
5. **Capability gate.** The section appears only when capabilities advertise the Automation
   API.

## Current code reality

- The console is served from `console.py` (`gateway_console_html`, `:198`) with its UI
  script in `console_ui.py`; it has no automations or schedule-specific section today.

## Validation

- `tests/test_console_automations.py`: inventory renders active, paused and legacy rows
  distinctly; pause/archive issue the right command types and surface rejections;
  a second principal's automations are never listed; the section is hidden when the
  capability is absent.

## Dependencies

- 0928 released (routes, shapes, capability entry).
