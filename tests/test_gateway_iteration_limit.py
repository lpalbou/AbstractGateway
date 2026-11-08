"""The gateway iteration default `agents.max_iterations` (operator 2026-11-08: "a way to configure
the number of iterations allowed for an agent"): the runtime-config door (read with its source,
write validated with a sentence, a warning past 200 but never a refusal), the CLI row, the
`/capabilities` descriptor every app reads, the `/runs/start` ladder (request > gateway default >
the workflow's own) with its `limits` receipt on the response and the run, the two backlog helper
routes' ladder (request > gateway default > the framework default), and the console field. Unset = the
framework default, AbstractRuntime's DEFAULT_MAX_ITERATIONS (50; operator 2026-11-08: "By default, set it to 50
everywhere"), served by name and never copied."""

from __future__ import annotations

import inspect
import json

import pytest

from test_gateway_live_delta_stream import HEADERS, _Ctl, _run_vars, gw  # noqa: F401 - the hermetic gateway fixture

pytestmark = pytest.mark.basic

CFG = "/api/gateway/admin/runtime-config"


def _start(client, **input_data):
    r = client.post("/api/gateway/runs/start", headers=HEADERS,
                    json={"bundle_id": "live", "flow_id": "root", "input_data": input_data})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_setting_reads_with_its_source_and_refuses_non_integers_in_words(gw, capsys) -> None:  # noqa: F811
    client, runtime_dir = gw
    row = client.get(CFG, headers=HEADERS).json()["agents"]["max_iterations"]
    assert row["key"] == "agents.max_iterations" and row["value"] is None and row["source"] == "default"
    assert row["default"] == 50, "unset shows the framework default"
    caps = client.get("/api/gateway/discovery/capabilities", headers=HEADERS).json()["capabilities"]
    assert caps["agent_limits"]["max_iterations"]["default"] == 50
    assert caps["agent_limits"]["max_iterations"]["source"] == "default"

    for bad in (True, 12.0, 0, -3, "twelve", "1.5"):
        r = client.post(CFG, headers=HEADERS, json={"agents": {"max_iterations": bad}})
        assert r.status_code == 400, (bad, r.text)
        assert "agents.max_iterations is a whole number of 1 or more" in r.json()["detail"], r.text

    ok = client.post(CFG, headers=HEADERS, json={"agents": {"max_iterations": 12}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["applied"] == {"agents.max_iterations": 12}
    row = client.get(CFG, headers=HEADERS).json()["agents"]["max_iterations"]
    assert row["value"] == 12 and row["source"] == "stored" and "warning" not in row
    caps = client.get("/api/gateway/discovery/capabilities", headers=HEADERS).json()["capabilities"]
    assert caps["agent_limits"]["max_iterations"] == {**caps["agent_limits"]["max_iterations"], "default": 12, "source": "stored", "run_field": "_limits.max_iterations"}

    big = client.post(CFG, headers=HEADERS, json={"agents.max_iterations": 500})
    assert big.status_code == 200, "no upper wall"
    assert "500 iterations is a lot" in big.json()["agents"]["max_iterations"]["warning"]

    from abstractgateway.config_cli import main as config_main

    config_main(["get", "agents.max_iterations", "--data-dir", str(runtime_dir), "--json"])
    assert json.loads(capsys.readouterr().out)["value"] == 500
    cleared = client.post(CFG, headers=HEADERS, json={"agents": {"max_iterations": None}})
    assert cleared.json()["agents"]["max_iterations"]["source"] == "default"


def test_run_start_ladder_and_receipt(gw) -> None:  # noqa: F811
    client, _ = gw
    _Ctl.reset(hold=False)
    none = _start(client, prompt="a")
    assert none["limits"] == {"max_iterations": 50, "source": "builtin"}
    none_vars = _run_vars(none["run_id"])
    assert none_vars["_gateway_limits"] == {"max_iterations": 50, "source": "builtin"}
    assert none_vars["_limits"]["max_iterations"] == 50, "the receipt is the budget the run holds"

    assert client.post(CFG, headers=HEADERS, json={"agents": {"max_iterations": 12}}).status_code == 200
    gd = _start(client, prompt="b")
    assert gd["limits"] == {"max_iterations": 12, "source": "gateway_default"}
    run_vars = _run_vars(gd["run_id"])
    assert run_vars["_limits"]["max_iterations"] == 12, "the default reaches the runtime's budget"
    assert run_vars["_gateway_limits"] == {"max_iterations": 12, "source": "gateway_default"}

    mine = _start(client, prompt="c", _limits={"max_iterations": 40})
    assert mine["limits"] == {"max_iterations": 40, "source": "request"}
    assert _run_vars(mine["run_id"])["_limits"]["max_iterations"] == 40
    flat = _start(client, prompt="d", max_iterations=7)
    assert flat["limits"] == {"max_iterations": 7, "source": "request"}

    forged = _start(client, prompt="e", _gateway_limits={"max_iterations": 999, "source": "request"})
    assert _run_vars(forged["run_id"])["_gateway_limits"] == {"max_iterations": 12, "source": "gateway_default"}


def test_backlog_helpers_use_the_one_ladder_and_the_framework_default(tmp_path) -> None:
    from abstractgateway.routes import gateway as gw_routes
    from abstractgateway.runtime_config import resolve_run_iteration_limit, write_runtime_config
    from abstractruntime import DEFAULT_MAX_ITERATIONS

    assert DEFAULT_MAX_ITERATIONS == 50
    assert resolve_run_iteration_limit(None, tmp_path) == {"max_iterations": 50, "source": "builtin"}
    assert resolve_run_iteration_limit(5, tmp_path) == {"max_iterations": 5, "source": "request"}
    write_runtime_config(tmp_path, {"agents": {"max_iterations": 30}}, actor="test")
    assert resolve_run_iteration_limit(None, tmp_path) == {"max_iterations": 30, "source": "gateway_default"}
    maintain = inspect.getsource(gw_routes.backlog_maintain)
    advisor = inspect.getsource(gw_routes.backlog_advisor)
    assert "resolve_run_iteration_limit(req.max_iterations, Path(svc.config.data_dir))" in maintain
    assert '"max_iterations": maintain_limits["max_iterations"]' in maintain and "limits=maintain_limits" in maintain
    assert "resolve_run_iteration_limit(req.max_iterations, Path(svc.config.data_dir))" in advisor
    assert '"max_iterations": advisor_limits["max_iterations"]' in advisor and "limits=advisor_limits" in advisor


_PRELUDE = """
const HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
"""


def test_console_iteration_field_saves_verifies_and_refuses_in_words() -> None:
    from test_gateway_console_offline import _console_script, _node, _slice_function

    source = _console_script()
    assert "iterationLimitMarkup(agentDefStore.data, agentDefStore)" in _slice_function(source, "agentDefaultsRender")
    mount = _slice_function(source, "mountAgentDefaults")
    assert "iterationLimitSave(" in mount
    # Typing never re-renders the block (that swallowed the Save click in the screenshot run).
    assert 'agentDefStore.iterDraft = t.value;\n' in mount and "iterationLimitWarning(t.value" in mount
    harness = _PRELUDE + f"""
const ITERATION_LIMIT_LABEL = "Iteration limit";
const ITERATION_LIMIT_DESC = "How many cycles.";
{_slice_function(source, "iterationLimitWarning")}
{_slice_function(source, "iterationLimitMarkup")}
{_slice_function(source, "iterationLimitBody")}
{_slice_function(source, "iterationLimitSave")}
const calls = [];
let posted = null, kept = null, fail = null;
async function api(path, opts) {{
  calls.push({{ path, method: (opts && opts.method) || "GET", body: opts && opts.body ? JSON.parse(opts.body) : null }});
  if (opts && opts.method === "POST") {{ if (fail) throw fail; posted = JSON.parse(opts.body); return {{}}; }}
  return {{ writable: true, agents: {{ max_iterations: row(kept, kept === null ? "default" : "stored") }} }};
}}
function agentDefaultsRender() {{}}
const row = (value, source) => ({{ key: "agents.max_iterations", value, source, default: 50, warn_above: 200 }});
const agentDefStore = {{ data: {{ writable: true, agents: {{ streaming_default: {{ value: false }}, max_iterations: row(null, "default") }} }}, iterSaving: false, iterSaved: null, iterDraft: null }};
const out = {{}};
out.unset = iterationLimitMarkup(agentDefStore.data, {{}});
out.set = iterationLimitMarkup({{ writable: true, agents: {{ max_iterations: row(12, "stored") }} }}, {{}});
out.big = iterationLimitMarkup({{ writable: true, agents: {{ max_iterations: row(12, "stored") }} }}, {{ iterDraft: "300" }});
out.viewer = iterationLimitMarkup({{ writable: false, agents: {{ max_iterations: row(12, "stored") }} }}, {{}});
out.missing = iterationLimitMarkup({{ writable: true, agents: {{}} }}, {{}});
out.bodies = [iterationLimitBody(""), iterationLimitBody("12"), iterationLimitBody("1.5"), iterationLimitBody("0")];
kept = 12;
await iterationLimitSave("12");
out.saved = {{ calls: calls.slice(), saved: agentDefStore.iterSaved, streaming: "streaming_default" in agentDefStore.data.agents }};
calls.length = 0;
await iterationLimitSave("abc");
out.local = {{ calls: calls.length, saved: agentDefStore.iterSaved }};
kept = 12;
await iterationLimitSave("30");
out.notKept = agentDefStore.iterSaved;
fail = Object.assign(new Error("400"), {{ data: {{ detail: "agents.max_iterations is a whole number of 1 or more (got 0)" }} }});
await iterationLimitSave("40");
out.refused = agentDefStore.iterSaved;
console.log(JSON.stringify([out]));
"""
    out = _node(harness)[0]
    assert 'type="number"' in out["unset"] and 'min="1"' in out["unset"] and "Iteration limit" in out["unset"]
    assert "Not set: the framework default (50)" in out["unset"] and "data-iteration-limit-clear" not in out["unset"]
    assert 'placeholder="Default (50)"' in out["unset"]
    assert 'value="12"' in out["set"] and "data-iteration-limit-clear" in out["set"] and "12 (saved on this gateway)" in out["set"]
    assert "data-iteration-limit-warning>" in out["big"] and "300 iterations is a lot" in out["big"]
    assert "data-iteration-limit-warning hidden>" in out["set"]
    assert "Only an admin can change this." in out["viewer"] and " disabled" in out["viewer"]
    assert "data-iteration-limit-missing" in out["missing"] and "not available on this gateway" in out["missing"]
    assert out["bodies"][0] == {"agents": {"max_iterations": None}} and out["bodies"][1] == {"agents": {"max_iterations": 12}}
    assert "error" in out["bodies"][2] and "error" in out["bodies"][3]
    saved = out["saved"]
    assert [c["method"] for c in saved["calls"]] == ["POST", "GET"], "the save is verified by a read"
    assert saved["calls"][0]["body"] == {"agents": {"max_iterations": 12}}
    assert saved["saved"]["tone"] == "ok" and "12 iterations" in saved["saved"]["text"] and saved["streaming"]
    assert out["local"]["calls"] == 0 and "whole number of 1 or more" in out["local"]["saved"]["text"]
    assert out["notKept"]["tone"] == "err" and "reports 12" in out["notKept"]["text"]
    assert out["refused"] == {"tone": "err", "head": "Not saved", "text": "agents.max_iterations is a whole number of 1 or more (got 0)"}
