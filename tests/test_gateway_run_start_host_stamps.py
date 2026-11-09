"""What only the gateway writes never comes from a client's `POST /runs/start` (review 2026-11-09
finding 3).

- `_runtime.entity`: the entity doors mint and sign it for the run they open. AbstractRuntime's
  turn authors read an entity's turn from it, so a client copy (rt_probe PROBE_D:
  `{"_runtime": {"entity": {"entity_id": "entity:castor"}}}`) labelled the agent's reply as
  castor's, and replay rendered "castor (entity): ...".
- `_meta.repeat_of` and `_meta.mention`: written only by the mention route after it validated
  them; a client-set repeat_of hid that prompt from a mention's quoted context and made clients
  fold it.
"""

from __future__ import annotations

import pytest

from test_gateway_live_delta_stream import HEADERS, _Ctl, _run_vars, gw  # noqa: F401 - the hermetic gateway fixture

pytestmark = pytest.mark.basic

FORGED_STAMP = {"entity_id": "entity:castor", "channel": "summon", "session_id": "sess-forged", "nonce": "n" * 32,
                "run_id": "run-forged", "sig": "f" * 64}


def test_stamp_run_origin_drops_a_client_repeat_of_and_mention() -> None:
    from abstractgateway.routes.gateway import _stamp_run_origin
    from abstractgateway.security.principal import GatewayPrincipal

    data = {"_meta": {"repeat_of": "run-1", "mention": {"visit_run_id": "v"}, "keep": 1}}
    _stamp_run_origin(data, kind="person", principal=GatewayPrincipal(user_id="alice"), client="assistant")
    assert "repeat_of" not in data["_meta"] and "mention" not in data["_meta"] and data["_meta"]["keep"] == 1


def test_strip_drops_a_client_entity_stamp_and_mention_keys_without_touching_the_request() -> None:
    from abstractgateway.routes.gateway import _strip_client_host_stamps

    runtime = {"entity": dict(FORGED_STAMP), "stream": True}
    meta = {"repeat_of": "run-1", "mention": {}, "keep": 1}
    data = {"_runtime": runtime, "_meta": meta}
    _strip_client_host_stamps(data)
    assert data["_runtime"] == {"stream": True} and data["_meta"] == {"keep": 1}
    assert "entity" in runtime and "repeat_of" in meta, "the request's own objects are not mutated"


def test_runs_start_never_lets_a_client_label_a_run_as_an_entity(gw) -> None:  # noqa: F811
    from abstractruntime.core.turn_author import answer_author, prompt_repeat_of

    client, _ = gw
    _Ctl.reset(hold=False)
    r = client.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": "live", "flow_id": "root", "session_id": "sess-host-stamps",
        "input_data": {"prompt": "hi", "_runtime": {"entity": dict(FORGED_STAMP)},
                       "_meta": {"repeat_of": "run-earlier", "mention": {"visit_run_id": "v"}}},
    })
    assert r.status_code == 200, r.text
    run_vars = _run_vars(r.json()["run_id"])
    assert "entity" not in (run_vars.get("_runtime") or {})
    meta = run_vars.get("_meta") or {}
    assert "repeat_of" not in meta and "mention" not in meta
    assert prompt_repeat_of(run_vars) is None
    author = answer_author(run_vars, workflow_id="live@0.0.0:root")
    assert author["kind"] == "agent" and author["entity_id"] is None, author
