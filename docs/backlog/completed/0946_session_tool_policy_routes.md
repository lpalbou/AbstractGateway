# 0946-gateway: [FEATURE] Session tool policy routes: approve or deny for the whole conversation, server-side

> Package: abstractgateway (routes, host wiring); abstractruntime (the store and the decision, its 0853)
> Type: feature
> Status: completed (2026-10-27, 0.16.0)
> Created: 2026-10-27
> Priority: P1
> Labels: tool-approval, sessions, ledger, multi-client
> Root: framework 1061; related framework 0993 (one ledger replay, many views)

## Summary

`GET`/`PUT`/`POST`/`DELETE /api/gateway/sessions/{session_id}/tool-policy`: a conversation's
standing "approve / deny without asking", clamped to the tools the gateway authorizes for the
caller, stored per account beside the run store, and read by the runtime before any approval
wait. The approval gate no longer flashes in apps after "approve all".

## Why

Operator, 2026-10-27, verbatim: "when a user gives permission to either all tools of a kind (e.g.
fetch url) OR all tools (all available/authorized for the user and authorized by the gateway), I
still see very quickly the gate approval showing up in apps and disappearing. Before sending the
gate approval, check whether the tool is already approved for the current session, and if so, do
not send the gate approval at all."

Before: the gateway injected only a start-time `_runtime.tool_policy` (`tool_grants.py`
`inject_default_grant`) and the host's static `ApprovalToolExecutor(policy=...)`; apps kept
"approve all" locally and answered each wait after it arrived.

## What ships (branch `feat/session-tool-policy`)

- `src/abstractgateway/session_tool_policy.py`: store per data root, `attach_session_tool_policy_store`,
  request normalization/merge, `build_policy` (expansion and clamp against `GET /discovery/tools`
  enabled rows), `capability()`.
- `src/abstractgateway/routes/session_tool_policy.py`: the four routes, audit lines.
- `hosts/bundle_host.py`: every runtime the host builds gets the store (no try/except: a runtime
  without it fails at host build); `live_deltas.require_runtime_features` probes it too.
- `capabilities.session_tool_policy` on `GET /discovery/capabilities`.
- Docs: `docs/api.md#session-tool-policy-approve-or-deny-for-the-whole-conversation`.
- Tests: `tests/test_session_tool_policy_routes.py` through the real app (TestClient, offline
  model): a kind approved once, the next call (made by the delegated agent child) runs with no
  waiting record or waiting snapshot anywhere in the run tree and one audit record; reset asks
  again; a tool the account is not authorized for is clamped and still refused; `all` expands to
  enabled rows only; deny; validation, merge, revision conflicts. Mutation-checked (host attach,
  clamp, enabled filter, runtime lowering).

## Remaining

- [x] `ABSTRACTRUNTIME_FLOOR` and the pyproject pins raised to AbstractRuntime 0.14.0 (it carries 0853).
- Live check on the operator's gateway with panel-chat (AbstractUIC PR) and the desktop Assistant
  (AbstractAssistant PR): approve a kind, watch no gate appear on any connected client.
- Follow-ups listed in root 1061 (iPhone/watch, AbstractCode web and terminal, the replay view's
  rendering of `abstract.tool_policy_decision`).

## Acceptance criteria

Completed 2026-10-27 (gateway part): PR #9 merged (`88fa1d88`), review fixes `046b0ff6`, host-build
refusal `3fa449a5`; released in 0.16.0. The live multi-client check and the client follow-ups (root 1061)
belong to the client repositories and stay open there.


- [x] Routes, clamp, store attach, capability, docs, tests.
- [x] Runtime floor raised to 0.14.0.
- [ ] Live multi-client check.
