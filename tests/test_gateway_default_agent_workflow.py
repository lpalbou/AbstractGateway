"""Default agent workflow per agent interface (`agents.default_workflow`).

Pins (CONTRACTS §D):
- the resolver: saved value > built-in default; a saved value that does not
  resolve is UNAVAILABLE with the reason — never a fallback to the built-in;
- write validation (every door): must resolve here and declare the interface;
- GET/POST /admin/runtime-config carry the `agents` block;
- /bundles and /workflow-catalog carry `default_agent_workflows` and mark
  the entrypoint `is_agent_default`;
- /runs/start and /runs/schedule accept flow_id "@default" (+ interface),
  answer `resolved_workflow` on every start, and refuse with 409 naming the
  setting when the default cannot run;
- the CLI door `config get|set|unset agents.default_workflow.<interface>`.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

CODE = "abstractcode.agent.v1"
ASSIST = "abstractassistant.agent.v1"


def write_bundle(
    bundles_dir: Path,
    *,
    bundle_id: str,
    version: str,
    entrypoints: List[Dict[str, Any]],
    default_entrypoint: Optional[str] = None,
) -> Path:
    """A minimal runnable .flow: each entrypoint is start -> end."""
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flows = {}
    for ep in entrypoints:
        fid = ep["flow_id"]
        flows[fid] = {
            "id": fid,
            "name": ep.get("name", fid),
            "description": "",
            "interfaces": list(ep.get("interfaces") or []),
            "nodes": [
                {"id": "n1", "type": "on_flow_start", "position": {"x": 0, "y": 0},
                 "data": {"nodeType": "on_flow_start", "label": "start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
                {"id": "n2", "type": "on_flow_end", "position": {"x": 200, "y": 0},
                 "data": {"nodeType": "on_flow_end", "label": "end", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
            ],
            "edges": [{"id": "e1", "source": "n1", "sourceHandle": "exec-out", "target": "n2", "targetHandle": "exec-in"}],
            "entryNode": "n1",
        }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": version,
        "created_at": "2026-09-25T00:00:00+00:00",
        "entrypoints": [
            {"flow_id": ep["flow_id"], "name": ep.get("name", ep["flow_id"]), "description": "", "interfaces": list(ep.get("interfaces") or [])}
            for ep in entrypoints
        ],
        "flows": {fid: f"flows/{fid}.json" for fid in flows},
        "artifacts": {},
        "assets": {},
        "metadata": {"lifecycle": {"channel": "draft" if version.startswith("draft.") else "published"}},
    }
    if default_entrypoint:
        manifest["default_entrypoint"] = default_entrypoint
    path = bundles_dir / f"{bundle_id}@{version}.flow"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for fid, flow in flows.items():
            zf.writestr(f"flows/{fid}.json", json.dumps(flow))
    return path


def standard_bundles(bundles_dir: Path) -> None:
    write_bundle(bundles_dir, bundle_id="basic-agent", version="0.0.1",
                 entrypoints=[{"flow_id": "ba", "name": "Basic agent", "interfaces": [CODE]}], default_entrypoint="ba")
    write_bundle(bundles_dir, bundle_id="coder", version="1.0.0",
                 entrypoints=[{"flow_id": "code", "name": "Coder", "interfaces": [CODE]},
                              {"flow_id": "plain", "name": "Plain", "interfaces": []}], default_entrypoint="code")
    write_bundle(bundles_dir, bundle_id="coder", version="1.1.0",
                 entrypoints=[{"flow_id": "code", "name": "Coder 1.1", "interfaces": [CODE]},
                              {"flow_id": "plain", "name": "Plain", "interfaces": []}], default_entrypoint="code")
    write_bundle(bundles_dir, bundle_id="coder", version="draft.abc",
                 entrypoints=[{"flow_id": "code", "name": "Coder draft", "interfaces": [CODE]}], default_entrypoint="code")


# ------------------------------------------------------------------ resolver


def _index(tmp_path: Path) -> list:
    from abstractgateway.agent_defaults import disk_entrypoint_index

    d = tmp_path / "bundles"
    standard_bundles(d)
    return disk_entrypoint_index([d])


def test_parse_workflow_ref() -> None:
    from abstractgateway.agent_defaults import DefaultWorkflowError, format_workflow_ref, parse_workflow_ref

    assert parse_workflow_ref("coder:code") == ("private", "coder", None, "code")
    assert parse_workflow_ref(" coder@1.0.0:code ") == ("private", "coder", "1.0.0", "code")
    assert parse_workflow_ref("private:coder:code") == ("private", "coder", None, "code")
    assert parse_workflow_ref("catalog:coder@2:code") == ("tenant_catalog", "coder", "2", "code")
    assert format_workflow_ref("coder", "2", "code", "tenant_catalog") == "catalog:coder@2:code"
    # A flow id may contain ':' (split on the FIRST ':' after the scope word).
    assert parse_workflow_ref("coder:ns:flow") == ("private", "coder", None, "ns:flow")
    assert parse_workflow_ref("catalog:coder@1:a:b") == ("tenant_catalog", "coder", "1", "a:b")
    assert parse_workflow_ref("tenant:coder:code") == ("private", "tenant", None, "coder:code")
    for bad in ("coder", ":code", "coder@:code", "coder:", "a@b@c:d", "", None):
        with pytest.raises(DefaultWorkflowError):
            parse_workflow_ref(bad)


def test_builtin_defaults_and_saved_precedence(tmp_path: Path) -> None:
    from abstractgateway.agent_defaults import Resolved, Unavailable, resolve_default_agent_workflow

    idx = _index(tmp_path)
    code = resolve_default_agent_workflow(CODE, index=idx, stored={})
    assert isinstance(code, Resolved) and code.source == "default"
    assert code.workflow_id == "basic-agent@0.0.1:ba" and code.name == "Basic agent"

    # The Assistant has no host default: unset = unavailable, in these words.
    assist = resolve_default_agent_workflow(ASSIST, index=idx, stored={})
    assert isinstance(assist, Unavailable) and assist.source == "default"
    assert assist.reason == (
        "no host workflow declares abstractassistant.agent.v1; the Assistant uses its built-in orchestrator"
    )

    # When a host workflow declares the Assistant interface, the reason says a
    # choice exists (still no automatic default).
    from abstractgateway.agent_defaults import _row

    with_assist = idx + [dict(_row(bundle_id="orch", bundle_version="1.0.0", ep={"flow_id": "m", "interfaces": [ASSIST]},
                                   default_entrypoint="m", registry_scope="private", deprecated=False, deprecated_reason=None),
                              is_latest=True)]
    a2 = resolve_default_agent_workflow(ASSIST, index=with_assist, stored={})
    assert isinstance(a2, Unavailable) and a2.reason.startswith("no default is set for abstractassistant.agent.v1 (1 workflow(s)")
    assert "built-in orchestrator" in a2.reason

    # No basic-agent on the host: the code default is unavailable, never guessed.
    no_basic = [r for r in idx if r["bundle_id"] != "basic-agent"]
    gone = resolve_default_agent_workflow(CODE, index=no_basic, stored={})
    assert isinstance(gone, Unavailable) and "'basic-agent' is not on this gateway" in gone.reason

    # Saved beats built-in; version-less follows the latest PUBLISHED (drafts never).
    saved = resolve_default_agent_workflow(CODE, index=idx, stored={CODE: "coder:code"})
    assert isinstance(saved, Resolved) and saved.source == "stored"
    assert saved.bundle_version == "1.1.0" and saved.name == "Coder 1.1"
    pinned = resolve_default_agent_workflow(CODE, index=idx, stored={CODE: "coder@1.0.0:code"})
    assert pinned.bundle_version == "1.0.0"


@pytest.mark.parametrize(
    "value, words",
    [
        ("missing:x", "is not on this gateway"),
        ("coder@9.9.9:code", "no published version 9.9.9"),
        ("coder@draft.abc:code", "no published version draft.abc"),
        ("coder:nope", "has no entrypoint 'nope'"),
        ("coder:plain", "declares no interface, not abstractcode.agent.v1"),
        ("catalog:coder:code", "is not on this gateway's tenant catalog"),
    ],
)
def test_saved_value_that_does_not_resolve_never_falls_back(tmp_path: Path, value: str, words: str) -> None:
    from abstractgateway.agent_defaults import Unavailable, resolve_default_agent_workflow

    res = resolve_default_agent_workflow(CODE, index=_index(tmp_path), stored={CODE: value})
    assert isinstance(res, Unavailable), "a broken saved value must NOT resolve to the built-in basic-agent"
    assert res.source == "stored" and res.value == value
    assert words in res.reason


def test_deprecated_entrypoint_is_unavailable(tmp_path: Path) -> None:
    from abstractgateway.agent_defaults import Unavailable, resolve_default_agent_workflow

    idx = _index(tmp_path)
    for r in idx:
        if r["bundle_id"] == "coder" and r["flow_id"] == "code":
            r["deprecated"], r["deprecated_reason"] = True, "replaced"
    res = resolve_default_agent_workflow(CODE, index=idx, stored={CODE: "coder:code"})
    assert isinstance(res, Unavailable) and "deprecated" in res.reason and "replaced" in res.reason


def test_validation_and_payload(tmp_path: Path) -> None:
    from abstractgateway.agent_defaults import DefaultWorkflowError, validate_default_workflow_value
    from abstractgateway.runtime_config import RuntimeConfigError, read_runtime_config, write_runtime_config

    idx = _index(tmp_path)
    assert validate_default_workflow_value(CODE, "coder:code", index=idx) == "coder:code"
    with pytest.raises(DefaultWorkflowError, match="declares no interface"):
        validate_default_workflow_value(CODE, "coder:plain", index=idx)

    data_dir = tmp_path / "data"
    with pytest.raises(RuntimeConfigError, match="refused"):
        write_runtime_config(data_dir, {f"agents.default_workflow.{CODE}": "coder:plain"}, actor="t", agent_index=idx)
    assert not (data_dir / "config" / "runtime_config.json").exists(), "a refused value never lands"

    out = write_runtime_config(data_dir, {"agents": {"default_workflow": {CODE: "coder:code"}}}, actor="t", agent_index=idx)
    assert out["applied"] == {f"agents.default_workflow.{CODE}": "coder:code"}
    row = out["agents"]["default_workflow"][CODE]
    assert row["source"] == "stored" and row["available"] is True
    assert row["resolved"]["workflow_id"] == "coder@1.1.0:code"
    assert row["key"] == f"agents.default_workflow.{CODE}" and row["default"] == "basic-agent:ba"
    assert {e["value"] for e in row["eligible"]} == {"basic-agent:ba", "coder:code"}
    assert list(out["agents"]["default_workflow"])[:2] == [CODE, ASSIST]

    # The per-knob resolvers read the same function: no agents scan there.
    assert "agents" not in read_runtime_config(data_dir)

    cleared = write_runtime_config(data_dir, {f"agents.default_workflow.{CODE}": ""}, actor="t", agent_index=idx)
    assert cleared["applied"] == {f"agents.default_workflow.{CODE}": None}
    assert cleared["agents"]["default_workflow"][CODE]["source"] == "default"


# ------------------------------------------------------------------ routes


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict]:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    standard_bundles(bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.delenv("ABSTRACTGATEWAY_STORE_BACKEND", raising=False)
    from abstractgateway.app import app

    return TestClient(app), {"Authorization": "Bearer t"}


def test_routes_settings_envelopes_and_default_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    with client:
        cfg = client.get("/api/gateway/admin/runtime-config", headers=h)
        assert cfg.status_code == 200, cfg.text
        agents = cfg.json()["agents"]
        assert agents["index_source"] == "host"
        assert agents["default_workflow"][CODE]["resolved"]["workflow_id"] == "basic-agent@0.0.1:ba"
        assert agents["default_workflow"][ASSIST]["available"] is False

        # Write: refused with the reason (400), then accepted.
        bad = client.post("/api/gateway/admin/runtime-config", headers=h,
                          json={"agents": {"default_workflow": {CODE: "coder:plain"}}})
        assert bad.status_code == 400 and "declares no interface" in bad.json()["detail"]
        ok = client.post("/api/gateway/admin/runtime-config", headers=h,
                         json={f"agents.default_workflow.{CODE}": "coder@1.0.0:code"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["agents"]["default_workflow"][CODE]["source"] == "stored"

        # Discovery envelopes (no admin needed for these routes).
        bundles = client.get("/api/gateway/bundles?all_versions=true", headers=h).json()
        d = bundles["default_agent_workflows"][CODE]
        assert d == {"workflow_id": "coder@1.0.0:code", "bundle_id": "coder", "bundle_version": "1.0.0", "flow_id": "code",
                     "registry_scope": "private", "name": "Coder", "source": "stored"}
        assert ASSIST in bundles["default_agent_workflows_unavailable"]
        marked = [(it["bundle_version"], ep["flow_id"]) for it in bundles["items"] for ep in it["entrypoints"] if ep["is_agent_default"]]
        assert marked == [("1.0.0", "code")]
        catalog = client.get("/api/gateway/workflow-catalog", headers=h).json()
        assert catalog["default_agent_workflows"][CODE]["workflow_id"] == "coder@1.0.0:code"

        # @default start: rewritten server-side, reported back.
        no_iface = client.post("/api/gateway/runs/start", headers=h, json={"flow_id": "@default", "input_data": {}})
        assert no_iface.status_code == 400 and "needs `interface`" in no_iface.json()["detail"]
        res = client.post("/api/gateway/runs/start", headers=h, json={"flow_id": "@default", "interface": CODE, "input_data": {}})
        assert res.status_code == 200, res.text
        rw = res.json()["resolved_workflow"]
        assert rw["source"] == "gateway_default" and rw["workflow_id"] == "coder@1.0.0:code" and rw["interface"] == CODE
        from abstractgateway.service import get_gateway_service

        run = get_gateway_service().host.run_store.load(res.json()["run_id"])
        assert run.workflow_id == "coder@1.0.0:code"
        # Persisted in the run (a restored conversation reads how it was chosen).
        sel = client.get(f"/api/gateway/runs/{res.json()['run_id']}/input_data", headers=h).json()["input_data"]["workflow_selection"]
        assert sel == {"workflow_id": "coder@1.0.0:code", "bundle_id": "coder", "bundle_version": "1.0.0", "flow_id": "code",
                       "registry_scope": "private", "name": "Coder", "source": "gateway_default", "interface": CODE}
        assert sel == rw

        # Every start reports what it runs.
        spoof = {"source": "gateway_default", "workflow_id": "evil@1:x"}
        plain = client.post("/api/gateway/runs/start", headers=h,
                            json={"bundle_id": "coder", "flow_id": "plain", "input_data": {"workflow_selection": spoof}})
        assert plain.status_code == 200, plain.text
        expected = {
            "workflow_id": "coder@1.1.0:plain", "bundle_id": "coder", "bundle_version": "1.1.0", "flow_id": "plain",
            "registry_scope": "private", "name": "Plain", "source": "client", "interface": None,
        }
        assert plain.json()["resolved_workflow"] == expected, "a client-sent workflow_selection is replaced"
        got = client.get(f"/api/gateway/runs/{plain.json()['run_id']}/input_data", headers=h).json()
        assert got["input_data"]["workflow_selection"] == expected

        # @default with bundle fields is a contradiction.
        both = client.post("/api/gateway/runs/start", headers=h, json={"flow_id": "@default", "interface": CODE, "bundle_id": "coder"})
        assert both.status_code == 400

        # An unavailable default refuses (409) naming the setting and its source.
        refused = client.post("/api/gateway/runs/start", headers=h,
                              json={"flow_id": "@default", "interface": ASSIST, "input_data": {}})
        assert refused.status_code == 409
        detail = refused.json()["detail"]
        assert f"agents.default_workflow.{ASSIST}" in detail and "source: default" in detail

        # Schedule accepts the same.
        sched_no_iface = client.post("/api/gateway/runs/schedule", headers=h, json={"flow_id": "@default", "input_data": {}})
        assert sched_no_iface.status_code == 400
        sched = client.post("/api/gateway/runs/schedule", headers=h, json={"flow_id": "@default", "interface": CODE, "input_data": {}})
        assert sched.status_code == 200, sched.text
        assert sched.json()["resolved_workflow"]["workflow_id"] == "coder@1.0.0:code"
        assert sched.json()["resolved_workflow"]["source"] == "gateway_default"
        parent = get_gateway_service().host.run_store.load(sched.json()["run_id"])
        assert parent.vars["_meta"]["schedule"]["target_workflow_id"] == "coder@1.0.0:code", (
            "the schedule must launch the version it reports"
        )
        assert parent.vars["workflow_selection"]["source"] == "gateway_default"
        assert parent.vars["vars"]["workflow_selection"]["workflow_id"] == "coder@1.0.0:code"
        sched_in = client.get(f"/api/gateway/runs/{sched.json()['run_id']}/input_data", headers=h).json()
        assert sched_in["input_data"]["workflow_selection"]["workflow_id"] == "coder@1.0.0:code"


def test_saved_default_whose_bundle_disappears_refuses_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    # Saved directly in the store (as if the bundle was removed after saving).
    store = tmp_path / "runtime" / "config" / "runtime_config.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"agents": {"default_workflow": {CODE: "gone:x"}}}), encoding="utf-8")
    with client:
        res = client.post("/api/gateway/runs/start", headers=h, json={"flow_id": "@default", "interface": CODE, "input_data": {}})
        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        assert "'gone' is not on this gateway" in detail and "source: stored" in detail and "gone:x" in detail
        # Discovery says the same thing instead of pointing at basic-agent.
        bundles = client.get("/api/gateway/bundles", headers=h).json()
        assert CODE not in bundles["default_agent_workflows"]
        assert bundles["default_agent_workflows_unavailable"][CODE]["source"] == "stored"


# ------------------------------------------------------------------ CLI


def test_cli_config_set_get_unset_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from abstractgateway import config_cli

    bundles_dir = tmp_path / "bundles"
    standard_bundles(bundles_dir)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    key = f"agents.default_workflow.{CODE}"

    with pytest.raises(SystemExit) as refused:
        config_cli.main(["set", key, "coder:plain", "--data-dir", str(data_dir)])
    assert refused.value.code == 2
    assert "declares no interface" in capsys.readouterr().err

    with pytest.raises(SystemExit) as ok:
        config_cli.main(["set", key, "coder:code", "--data-dir", str(data_dir)])
    assert ok.value.code == 0
    stored = json.loads((data_dir / "config" / "runtime_config.json").read_text())
    assert stored["agents"]["default_workflow"][CODE] == "coder:code"
    set_out = capsys.readouterr()
    assert "runs coder@1.1.0:code" in set_out.out and "choices on this gateway" in set_out.out

    config_cli.main(["get", key, "--data-dir", str(data_dir), "--json"])
    row = json.loads(capsys.readouterr().out)
    assert row["source"] == "stored" and row["resolved"]["workflow_id"] == "coder@1.1.0:code"

    with pytest.raises(SystemExit) as unset:
        config_cli.main(["unset", key, "--data-dir", str(data_dir)])
    assert unset.value.code == 0
    capsys.readouterr()
    config_cli.main(["get", key, "--data-dir", str(data_dir), "--json"])
    assert json.loads(capsys.readouterr().out)["source"] == "default"


def test_unknown_key_refuses_the_whole_write(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, write_runtime_config

    data_dir = tmp_path / "data"
    with pytest.raises(RuntimeConfigError, match="unknown setting.*nothing was saved"):
        write_runtime_config(data_dir, {"executor": "codex", "exectuor_typo": "claude"}, actor="t")
    assert not (data_dir / "config" / "runtime_config.json").exists()


def test_concurrent_writers_never_lose_a_change(tmp_path: Path) -> None:
    """Read-modify-replace under the store lock: N threads each saving a
    different key all land."""
    import threading

    from abstractgateway.runtime_config import _read_store, write_runtime_config

    data_dir = tmp_path / "data"
    keys = [("operator_email", "a@b.c"), ("stop_kill_switch_s", 3), ("executor", "claude"),
            ("workspace_default_mode", "blacklist"), ("allow_engine_install", True), ("trust_client_launch_folder", False)]
    errors: list = []

    def save(k, v):
        try:
            for _ in range(5):
                write_runtime_config(data_dir, {k: v}, actor="t")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=save, args=kv) for kv in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    stored = _read_store(data_dir)
    assert {k for k, _ in keys} <= set(stored)


# ------------------------------------------------------------------ console (WUI)


def test_console_default_agent_block_renders_and_posts_only_changes() -> None:
    """The Workflows tab block, on the SHIPPED source: one row per interface,
    the source pill, what it runs or why it cannot, a select of the
    gateway's eligible entrypoints, and a save body with the changed rows
    only ("" = back to the built-in default)."""
    from abstractgateway.console import gateway_console_html
    from test_gateway_console_offline import _console_script, _node, _slice_function

    html = gateway_console_html()
    assert 'id="agent-defaults-root"' in html and "Default agent workflow" in html
    source = _console_script()
    assert 'mountAgentDefaults("workflows", $("agent-defaults-root"))' in source
    assert "function agentDefaultCell(" in source and "Make agent default" in source
    harness = f"""
const HTML_ESCAPES = {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
{_slice_function(source, "uiPill")}
{_slice_function(source, "agentDefaultsBody")}
{_slice_function(source, "agentDefaultsMarkup")}
const agentDefStore = {{ data: null, error: "", saving: false, saved: null, draft: {{}}, views: new Map() }};
const block = {{
  label: "Default agent workflow", help: "h",
  default_workflow: {{
    "abstractcode.agent.v1": {{ value: "coder:code", source: "stored", available: true, reason: null, key: "agents.default_workflow.abstractcode.agent.v1",
      default: "basic-agent:ba", resolved: {{ workflow_id: "coder@1.1.0:code", name: "Coder" }},
      eligible: [{{ value: "basic-agent:ba", name: "Basic", bundle_version: "0.0.1", flow_id: "ba" }}, {{ value: "coder:code", name: "Coder", bundle_version: "1.1.0", flow_id: "code" }}] }},
    "abstractassistant.agent.v1": {{ value: null, source: "default", available: false, key: "agents.default_workflow.abstractassistant.agent.v1",
      reason: "no host workflow declares abstractassistant.agent.v1; the Assistant uses its built-in orchestrator", default: null, resolved: null, eligible: [] }},
  }},
}};
agentDefStore.data = {{ writable: true, agents: block }};
const html = agentDefaultsMarkup();
const out = [];
out.push({{ k: "html", code: html.includes("coder@1.1.0:code") && html.includes("Saved setting"),
  assist: html.includes("Not available: no host workflow declares abstractassistant.agent.v1"),
  none: html.includes("No workflow on this gateway declares this interface."),
  selected: html.includes('value="coder:code" selected'), save: html.includes("data-agent-defaults-save") }});
out.push({{ k: "same", body: agentDefaultsBody(block, {{ "abstractcode.agent.v1": "coder:code" }}) }});
out.push({{ k: "change", body: agentDefaultsBody(block, {{ "abstractcode.agent.v1": "basic-agent:ba" }}) }});
out.push({{ k: "clear", body: agentDefaultsBody(block, {{ "abstractcode.agent.v1": "" }}) }});
agentDefStore.data = {{ writable: false, agents: block }};
out.push({{ k: "readonly", html: agentDefaultsMarkup() }});
console.log(JSON.stringify(out));
"""
    rows = {r["k"]: r for r in _node(harness)}
    assert rows["html"] == {"k": "html", "code": True, "assist": True, "none": True, "selected": True, "save": True}
    assert rows["same"]["body"] is None
    assert rows["change"]["body"] == {"agents": {"default_workflow": {CODE: "basic-agent:ba"}}}
    assert rows["clear"]["body"] == {"agents": {"default_workflow": {CODE: ""}}}
    ro = rows["readonly"]["html"]
    assert "data-agent-defaults-save" not in ro and "Only an admin can change these." in ro and " disabled" in ro


# ------------------------------------------------------------------ telegram bridge


def test_telegram_bridge_unset_flow_follows_the_gateway_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ABSTRACT_TELEGRAM_FLOW_ID/BUNDLE_ID: the bridge starts `@default` +
    abstractcode.agent.v1 and the HOST resolves it at every message, so the
    run records source gateway_default; unavailable -> the chat is told."""
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    for k in ("ABSTRACT_TELEGRAM_FLOW_ID", "ABSTRACT_TELEGRAM_DEFAULT_FLOW_ID", "ABSTRACT_TELEGRAM_BUNDLE_ID"):
        monkeypatch.delenv(k, raising=False)
    cfg = TelegramBridgeConfig.from_env(base_dir=tmp_path)
    assert cfg.flow_id == "@default" and cfg.bundle_id is None
    bridge = TelegramBridge(config=cfg, host=None, runner=None, artifact_store=None)
    assert bridge._run_target(chat_id=1) == ("@default", None, {"interface": CODE}), "never a bundle picked client-side"


def test_host_start_resolves_default_and_records_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(tmp_path, monkeypatch)
    with client:
        from abstractgateway.agent_defaults import DefaultWorkflowUnavailable
        from abstractgateway.service import get_gateway_service

        host = get_gateway_service().host
        rid = host.start_run(flow_id="@default", interface=CODE, input_data={}, actor_id="gateway", session_id="tg:1")
        run = host.run_store.load(rid)
        assert run.workflow_id == "basic-agent@0.0.1:ba"
        assert run.vars["workflow_selection"]["source"] == "gateway_default"
        assert run.vars["workflow_selection"]["interface"] == CODE
        with pytest.raises(DefaultWorkflowUnavailable) as e:
            host.start_run(flow_id="@default", interface=ASSIST, input_data={}, actor_id="gateway")
        assert "agents.default_workflow.abstractassistant.agent.v1" in str(e.value)
        with pytest.raises(ValueError):
            host.start_run(flow_id="@default", input_data={}, actor_id="gateway")
