# A revised automation resolves the tool approvals it now grants (every client)

**Status**: completed (2026-10-29, unreleased) · **Priority**: P0 · **Created**: 2026-10-29
**Cross-package**: AbstractAssistant 1080 (its editor did the same at save time, client side; it now relies on
this rule and only refreshes). AbstractRuntime unchanged: its grant functions are reused as they are.

## Why (operator QA, 2026-10-29)
In the web console the operator edited the waiting automation "You report your daily news…" (occurrence #8,
"Approval needed") to Tools = "Run without asking" and saved. The run stayed "waiting for you": no client but the
desktop app answered the wait, and only at its own save. One rule, in one place: the gateway.

## What shipped
- `src/abstractgateway/automation_wait_resolver.py`:
  - `granted_tools(definition, untrusted_input=)` computes what an occurrence runs without asking, on a copy of the
    target inputs, with the runtime's `apply_named_tool_policy` / `grant_tool_approval`. Withheld sending tools and
    MCP tools on untrusted input are never granted.
  - `still_asks(definition, wait)` returns the wait's tools that are still not granted.
  - `resolve_covered_waits(run_store, command_store, automation_id, actor=)` handles each tool-approval wait of the
    automation's runs (`pending_waits`) whose every tool is granted. It appends a durable `resume` command
    `{wait_key, payload: {approved: true}}` with `client_id` set to the person who saved the revision. The command
    id is derived from the automation, the revision and the wait key, so a replayed revise answers nothing twice.
- `runner.py`: after an `automation.revise` is applied (not a duplicate), the runner calls the resolver. A failure is
  logged and never fails the revise; the wait stays answerable by hand.

## Evidence
- `tests/test_automations_tool_approval.py` (real gateway, runner on):
  - `test_a_revision_that_grants_the_waiting_tools_resolves_the_wait`: the operator's case, on the plain revise
    route that every client uses. The definition is "ask", the occurrence waits on execute_command, the revise sets
    "auto", the occurrence completes and the tool ran. It is red without the runner hook (mutation-checked).
  - `test_a_revision_that_still_asks_for_a_tool_leaves_the_wait`: per-tool approvals keep execute_command on Ask, so
    the run keeps waiting and `still_asks` names execute_command.
- The neighbouring suites (named tool policy, revise onto a connection, command types) pass.

## Follow-ups
- The status line naming the tool that keeps a run waiting needs `still_asks` in the automation summary's waits;
  the clients then show it.
