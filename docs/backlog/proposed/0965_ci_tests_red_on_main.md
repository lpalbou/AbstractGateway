# 0965 — Tests red on main (CI and local), filed instead of touched

> Package: abstractgateway
> Type: task
> Created: 2026-10-29
> Priority: high
> Labels: ci, flaky-tests, test-hygiene

## Summary

PR #21 (first-run onboarding) was merged with CI red on ONE test that also fails on pristine `main`; the
operator ruled to file it here rather than touch it in that PR. One place for every test that is red on
`main` for a reason outside the PR at hand.

## Items

| Test | Where it fails | Evidence | Notes |
|---|---|---|---|
| `tests/test_mail_collector_change_driven.py::test_400_finished_runs_a_change_to_one_loads_exactly_one` | CI on 3.10, 3.11, 3.12, 3.13 (run 40785179241, PR #21 head a75aec0a); locally on pristine `origin/main` 71d62877 | `assert 400 == 0`: the "full pass" loads all 400 finished RunStates instead of reading index rows | Deterministic, not flaky. Suspect: the run-store index contract with the installed AbstractRuntime (0.15.1 on PyPI) |
| `tests/test_push_forwarder_setting.py::test_a_split_runner_sees_the_settings_file_move` | CI 3.13 on `main` 71d62877 (run 40732044914) | `condition not met in time` | Timing; intermittent |
| `tests/test_gateway_default_agent_workflow.py::test_routes_settings_envelopes_and_default_start`, `tests/test_gateway_publish_without_rebuild.py::test_first_llm_workflow_on_a_plain_runtime_rebuilds_only_that_service` | locally on pristine `main` (macOS, the framework checkout) | — | Environment-dependent; green on CI |
| `tests/test_gateway_route_authorization_contract.py` (two cases) | locally, when the installed AbstractNote mounts `POST /api/gateway/note/claims/{claim_id}/retract` | a write route with no authorization decision | The note seam's route needs a GATEWAY_ROUTE_POLICIES row or a USER_LEVEL_WRITES entry |

## Acceptance

- Each row: root cause found, fixed (or the test corrected), and CI green on `main` for three consecutive runs.
