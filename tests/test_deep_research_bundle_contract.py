from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader
from abstractruntime.workflow_bundle import open_workflow_bundle

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "flows" / "bundles" / "deep-research@0.1.8.flow"
READ_ONLY_TOOLS = {
    "web_search",
    "fetch_url",
    "skim_websearch",
    "skim_url",
    "read_file",
    "skim_files",
}
FORBIDDEN_AGENT_TOOLS = {
    "execute_command",
    "write_file",
    "edit_file",
    "send_email",
    "send_telegram_message",
    "send_whatsapp_message",
}


def _stub_deep_llm_data(schema: dict | None) -> dict:
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    if "report_markdown" in required:
        return {
            "report_markdown": (
                "# Smoke Report\n\n"
                "Works as intended [s1].\n\n"
                "## Evidence Table\n\n"
                "| Claim | Source |\n"
                "| --- | --- |\n"
                "| Works | s1 |\n\n"
                "## Claim-Evidence Matrix\n\n"
                "- Internal audit row that should not be user-facing.\n\n"
                "## Findings\n\n"
                "The workflow exported the report [s1].\n\n"
                "## Citations / Source IDs\n\n"
                "- s1: Synthetic short citation that should be replaced by References.\n\n"
                "## References\n\n"
                "- [bad] Bad model-written reference that should be replaced.\n"
            ),
            "research_run_manifest": {"bundle_id": "deep-research", "status": "smoke"},
            "source_ledger": [
                {
                    "source_id": "s1",
                    "title": "Synthetic",
                    "url_or_path": "https://example.com/s1",
                    "source_type": "test",
                    "fetched": True,
                    "evidence_quality": "synthetic",
                    "relevance": "high",
                    "fetched_at": "2026-06-28T00:00:00Z",
                    "content_hash": "sha256:test",
                    "rejected_reason": "",
                }
            ],
            "claim_evidence_matrix": [
                {
                    "claim_id": "c1",
                    "claim": "Works",
                    "evidence_source_ids": ["s1"],
                    "confidence": "high",
                    "status": "supported",
                }
            ],
            "iteration_log": [
                {
                    "round_index": 0,
                    "queries": [],
                    "new_findings": ["Smoke"],
                    "review_guidance_used": [],
                    "remaining_gaps": [],
                    "continue_reason": "smoke",
                }
            ],
            "adversarial_review_summary": "No review rounds in smoke.",
            "limitations": [],
            "warnings": [],
            "export_status": {"smoke": True},
        }
    if "brief" in required:
        return {
            "brief": "smoke",
            "research_questions": ["q"],
            "source_strategy": ["s"],
            "quality_gates": ["g"],
            "stop_rules": ["r"],
        }
    if "answer_hypotheses" in required:
        return {
            "answer_hypotheses": ["Works"],
            "source_ledger": [
                {
                    "source_id": "s1",
                    "title": "Synthetic",
                    "url_or_path": "memory://s1",
                    "source_type": "test",
                    "fetched": True,
                    "evidence_quality": "synthetic",
                    "relevance": "high",
                    "fetched_at": "2026-06-28T00:00:00Z",
                    "content_hash": "sha256:test",
                    "rejected_reason": "",
                }
            ],
            "iteration_log": [
                {
                    "round_index": 0,
                    "queries": [],
                    "new_findings": ["Smoke"],
                    "review_guidance_used": [],
                    "remaining_gaps": [],
                    "continue_reason": "smoke",
                }
            ],
            "claim_candidates": [
                {
                    "claim_id": "c1",
                    "claim": "Works",
                    "evidence_source_ids": ["s1"],
                    "confidence": "high",
                    "status": "supported",
                }
            ],
            "open_questions": [],
            "limitations": [],
            "warnings": [],
        }
    if "verdict" in required:
        return {
            "verdict": "continue",
            "useful_insights": [],
            "investigate_next": [],
            "user_relevance": [],
            "risks": [],
            "continue_research": False,
        }
    return {}


def _tick_until_terminal(host, run_store, run_id: str, data_dir: Path) -> object:
    from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
    from abstractruntime.core.models import RunStatus

    runner = GatewayRunner(
        base_dir=data_dir,
        host=host,
        config=GatewayRunnerConfig(tick_max_steps=200, tick_workers=1),
        enable=False,
    )
    terminal = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    for _ in range(50):
        parent = run_store.load(run_id)
        if parent is not None and parent.status in terminal:
            return parent
        running = run_store.list_runs(status=RunStatus.RUNNING, limit=100)
        assert running, f"run {run_id} stalled without running descendants"
        for run in list(running):
            runner._tick_run(run.run_id)
    parent = run_store.load(run_id)
    raise AssertionError(f"run {run_id} did not finish; last state={parent}")


def _read_flow(bundle, flow_id: str) -> dict:
    relpath = bundle.manifest.flows[flow_id]
    raw = bundle.read_json(relpath)
    assert isinstance(raw, dict)
    return raw


def _node_type(node: dict) -> str:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    return str(node.get("type") or data.get("nodeType") or "")


def _pin_ids(node: dict, direction: str) -> set[str]:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    pins = data.get(direction)
    return (
        {str(pin.get("id")) for pin in pins if isinstance(pin, dict)}
        if isinstance(pins, list)
        else set()
    )


def _node_by_id(flow: dict, node_id: str) -> dict:
    return next(node for node in flow["nodes"] if node["id"] == node_id)


def _edge_handles(flow: dict) -> set[tuple[str, str, str, str]]:
    return {
        (
            str(edge.get("source")),
            str(edge.get("sourceHandle")),
            str(edge.get("target")),
            str(edge.get("targetHandle")),
        )
        for edge in flow.get("edges") or []
        if isinstance(edge, dict)
    }


def test_deep_research_bundle_manifest_and_entrypoint_contract() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    manifest = bundle.manifest

    assert manifest.bundle_id == "deep-research"
    assert manifest.bundle_version == "0.1.8"
    assert manifest.default_entrypoint == "deep-research"
    assert [ep.flow_id for ep in manifest.entrypoints] == ["deep-research"]
    assert set(manifest.flows) == {
        "deep-plan",
        "deep-investigate",
        "deep-review",
        "deep-render",
        "deep-research",
    }
    # Metadata block restored by flow's in-place repack (c2579 — the
    # dp->deep rename had dropped it accidentally; pack_deep_research_bundle
    # now owns publishing so a repack cannot forget it again).
    assert manifest.metadata["family"] == "deep-research"
    assert manifest.metadata["control_policy"]["user_budget_control"] == "effort"
    assert manifest.metadata["control_policy"]["outer_loop"] == "review_gated_while_with_effort_budget"
    assert manifest.metadata["default_model_profile"]["override_pins"] == ["provider", "model"]
    assert "docx_report" in manifest.metadata["outputs"]
    assert (
        manifest.metadata["tool_policy"]["research_agents"] == sorted(READ_ONLY_TOOLS)
        or set(manifest.metadata["tool_policy"]["research_agents"]) == READ_ONLY_TOOLS
    )


def test_deep_research_start_inputs_are_product_facing() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    root = _read_flow(bundle, "deep-research")
    start = next(node for node in root["nodes"] if node["id"] == "start")
    outputs = _pin_ids(start, "outputs")

    # `prompt` is the abstractcode.agent.v1 boundary pin: agent hosts send the
    # user's text there, direct callers send `request`.
    assert outputs == {"exec-out", "request", "viewpoint", "effort", "provider", "model", "prompt"}
    defaults = start["data"]["pinDefaults"]
    assert defaults == {"effort": "standard", "provider": "", "model": ""}


def test_deep_research_reads_the_agent_prompt_and_sets_success() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    root = _read_flow(bundle, "deep-research")
    edges = {
        (str(e.get("source")), str(e.get("sourceHandle")), str(e.get("target")), str(e.get("targetHandle")))
        for e in root.get("edges") or []
        if isinstance(e, dict)
    }
    # Both request inputs feed resolve_request (request, else prompt) ...
    assert ("start", "request", "resolve_request", "request") in edges
    assert ("start", "prompt", "resolve_request", "prompt") in edges
    # ... and no request consumer reads `request` from the start node directly.
    assert not [e for e in edges if e[0] == "start" and e[1] == "request" and e[2] != "resolve_request"]
    # The end node's `success` pin is wired (true when a report was produced).
    end = next(node for node in root["nodes"] if node["id"] == "end")
    assert "success" in _pin_ids(end, "inputs")
    assert ("report_success", "output", "end", "success") in edges


def test_deep_research_exports_markdown_pdf_docx_and_audit_files() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    root = _read_flow(bundle, "deep-research")
    node_types = {_node_type(node) for node in root["nodes"]}
    end = next(node for node in root["nodes"] if node["id"] == "end")
    end_inputs = _pin_ids(end, "inputs")

    assert {"write_file", "write_pdf", "write_docx"}.issubset(node_types)
    assert {
        "md_path",
        "pdf_path",
        "docx_path",
        "pdf_sha256",
        "docx_sha256",
        "manifest_path",
        "source_ledger_path",
        "claim_matrix_path",
        "iteration_log_path",
        "warnings_path",
        "export_manifest",
        "export_status",
        "report_markdown",
    }.issubset(end_inputs)
    assert "post_export_manifest" in {str(node.get("id")) for node in root["nodes"]}


def test_deep_research_root_enforces_review_gated_research_rounds() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    root = _read_flow(bundle, "deep-research")
    node_types = {str(node.get("id")): _node_type(node) for node in root["nodes"]}
    edges = _edge_handles(root)

    assert node_types["research_rounds"] == "while"
    assert node_types["derive_settings"] == "code"
    assert node_types["loop_condition"] == "code"
    assert node_types["next_loop_state"] == "code"
    assert node_types["set_latest_investigation"] == "set_var"
    assert node_types["set_latest_review"] == "set_var"
    assert node_types["set_loop_state"] == "set_var"
    assert ("start", "effort", "derive_settings", "effort") in edges
    assert ("start", "provider", "derive_settings", "provider") in edges
    assert ("start", "model", "derive_settings", "model") in edges
    assert ("derive_settings", "output", "get_max_review_rounds", "object") in edges
    assert ("get_loop_state", "value", "loop_condition", "loop_state") in edges
    assert ("get_max_review_rounds", "value", "loop_condition", "max_review_rounds") in edges
    assert ("loop_condition", "condition", "research_rounds", "condition") in edges
    assert ("research_rounds", "loop", "investigate", "exec-in") in edges
    assert ("investigate", "exec-out", "set_latest_investigation", "exec-in") in edges
    assert ("set_latest_investigation", "exec-out", "review", "exec-in") in edges
    assert ("review", "exec-out", "set_latest_review", "exec-in") in edges
    assert ("set_latest_review", "exec-out", "set_loop_state", "exec-in") in edges
    assert ("next_loop_state", "output", "set_loop_state", "value") in edges
    assert ("research_rounds", "done", "render", "exec-in") in edges
    assert ("research_rounds", "index", "investigate_input", "round_index") in edges
    assert ("get_max_review_rounds", "value", "investigate_input", "total_rounds") in edges
    assert ("get_prior_review", "value", "investigate_input", "adversarial_review") in edges
    assert ("get_final_review", "value", "render_input", "adversarial_review") in edges
    assert ("get_review_rounds_completed", "value", "render_input", "review_rounds_completed") in edges


def test_deep_research_agents_do_not_use_dangerous_tools() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    for flow_id in bundle.manifest.flows:
        flow = _read_flow(bundle, flow_id)
        for node in flow.get("nodes") or []:
            if not isinstance(node, dict) or _node_type(node) != "agent":
                continue
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            defaults = data.get("pinDefaults") if isinstance(data.get("pinDefaults"), dict) else {}
            tools = defaults.get("tools")
            assert isinstance(tools, list), f"{flow_id}:{node.get('id')} must pin tools"
            tool_set = {str(tool) for tool in tools}
            assert tool_set <= READ_ONLY_TOOLS
            assert not (tool_set & FORBIDDEN_AGENT_TOOLS)


def test_deep_research_bundle_does_not_wire_thinking_controls() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    for flow_id in bundle.manifest.flows:
        flow = _read_flow(bundle, flow_id)
        for node in flow.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            assert node.get("id") != "get_thinking", f"{flow_id} must not derive model thinking controls"
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            for direction in ("inputs", "outputs"):
                for pin in data.get(direction) or []:
                    if isinstance(pin, dict):
                        assert pin.get("id") != "thinking", f"{flow_id}:{node.get('id')} exposes thinking pin"
            defaults = data.get("pinDefaults") if isinstance(data.get("pinDefaults"), dict) else {}
            assert "thinking" not in defaults, f"{flow_id}:{node.get('id')} pins thinking default"
        for edge in flow.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            assert edge.get("sourceHandle") != "thinking", f"{flow_id} wires thinking source handle"
            assert edge.get("targetHandle") != "thinking", f"{flow_id} wires thinking target handle"


def test_deep_research_structured_outputs_include_audit_contracts() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    render = _read_flow(bundle, "deep-render")
    writer = _node_by_id(render, "writer")
    defaults = writer["data"]["pinDefaults"]
    schema = defaults["resp_schema"]
    system = defaults["system"].lower()
    required = set(schema["required"])

    assert {
        "report_markdown",
        "research_run_manifest",
        "source_ledger",
        "claim_evidence_matrix",
        "iteration_log",
        "adversarial_review_summary",
        "limitations",
        "warnings",
        "export_status",
    }.issubset(required)
    source_item = schema["properties"]["source_ledger"]["items"]
    claim_item = schema["properties"]["claim_evidence_matrix"]["items"]
    iteration_item = schema["properties"]["iteration_log"]["items"]
    assert {
        "source_id",
        "url_or_path",
        "fetched",
        "evidence_quality",
        "fetched_at",
    }.issubset(set(source_item["required"]))
    assert {"claim_id", "claim", "evidence_source_ids", "confidence", "status"}.issubset(
        set(claim_item["required"])
    )
    assert {
        "round_index",
        "new_findings",
        "review_guidance_used",
        "remaining_gaps",
    }.issubset(set(iteration_item["required"]))
    assert "inline source citations" in system
    assert "## references" in system
    assert "do not include visible sections named evidence table" in system
    assert "## references is the only source-list section" in system
    assert "machine-readable audit outputs" in system
    json.dumps(schema)


def test_deep_render_normalizes_user_facing_report_before_export() -> None:
    bundle = open_workflow_bundle(BUNDLE)
    render = _read_flow(bundle, "deep-render")
    edges = _edge_handles(render)
    normalize = _node_by_id(render, "normalize_report_markdown")
    code_body = str(normalize["data"].get("codeBody") or "").lower()

    assert _node_type(normalize) == "code"
    assert ("get_report_markdown", "value", "normalize_report_markdown", "report_markdown") in edges
    assert ("get_source_ledger", "value", "normalize_report_markdown", "source_ledger") in edges
    assert ("normalize_report_markdown", "output", "end", "report_markdown") in edges
    assert ("get_report_markdown", "value", "end", "report_markdown") not in edges
    assert "citations source ids" in code_body
    assert "source ids" in code_body
    assert "references" in code_body


def test_deep_research_bundle_loads_through_gateway_bundle_host(tmp_path: Path) -> None:
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractruntime.storage.artifacts import InMemoryArtifactStore
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    bundles_dir = tmp_path / "bundles"
    data_dir = tmp_path / "data"
    bundles_dir.mkdir()
    shutil.copy2(BUNDLE, bundles_dir / BUNDLE.name)

    host = WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=data_dir,
        run_store=InMemoryRunStore(),
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )

    assert getattr(host, "_default_bundle_id", None) == "deep-research"
    assert {
        "deep-research@0.1.8:deep-plan",
        "deep-research@0.1.8:deep-investigate",
        "deep-research@0.1.8:deep-review",
        "deep-research@0.1.8:deep-render",
        "deep-research@0.1.8:deep-research",
    }.issubset(set(host.specs))


def test_deep_research_mocked_run_exports_report_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractruntime.core.models import EffectType, RunStatus
    from abstractruntime.core.runtime import EffectOutcome, Runtime
    from abstractruntime.integrations.abstractcore import factory as ac_factory
    from abstractruntime.storage.artifacts import InMemoryArtifactStore
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    bundles_dir = tmp_path / "bundles"
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    bundles_dir.mkdir()
    workspace.mkdir()
    shutil.copy2(BUNDLE, bundles_dir / BUNDLE.name)

    def _fake_create_local_runtime(**kwargs):
        def _llm_stub(run, effect, default_next_node):
            del run, default_next_node
            payload = dict(effect.payload or {})
            data = _stub_deep_llm_data(payload.get("response_schema"))
            return EffectOutcome.completed(
                {
                    "content": json.dumps(data, separators=(",", ":")),
                    "data": data,
                    "tool_calls": [],
                    "model": payload.get("model"),
                    "provider": payload.get("provider"),
                }
            )

        return Runtime(
            run_store=kwargs["run_store"],
            ledger_store=kwargs["ledger_store"],
            artifact_store=kwargs.get("artifact_store") or InMemoryArtifactStore(),
            effect_handlers={EffectType.LLM_CALL: _llm_stub},
        )

    monkeypatch.setattr(ac_factory, "create_local_runtime", _fake_create_local_runtime)

    run_store = InMemoryRunStore()
    ledger_store = InMemoryLedgerStore()
    artifact_store = InMemoryArtifactStore()
    host = WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=data_dir,
        run_store=run_store,
        ledger_store=ledger_store,
        artifact_store=artifact_store,
    )

    run_id = host.start_run(
        flow_id="deep-research",
        bundle_id="deep-research",
        input_data={
            "request": "smoke",
            "viewpoint": "test",
            "effort": "quick",
            "workspace_root": str(workspace),
            "workspace_access_mode": "all_except_ignored",
        },
    )

    final = _tick_until_terminal(host, run_store, run_id, data_dir)
    assert final.status == RunStatus.COMPLETED
    assert isinstance(final.output, dict)
    output = final.output

    for key in [
        "md_path",
        "pdf_path",
        "docx_path",
        "manifest_path",
        "source_ledger_path",
        "claim_matrix_path",
        "iteration_log_path",
        "warnings_path",
    ]:
        relpath = output[key]
        assert isinstance(relpath, str)
        assert (workspace / relpath).is_file(), key

    export_bases = {
        output["md_path"].removesuffix(".md"),
        output["pdf_path"].removesuffix(".pdf"),
        output["docx_path"].removesuffix(".docx"),
        output["manifest_path"].removesuffix(".manifest.json"),
        output["source_ledger_path"].removesuffix(".sources.json"),
        output["claim_matrix_path"].removesuffix(".claims.json"),
        output["iteration_log_path"].removesuffix(".iterations.json"),
        output["warnings_path"].removesuffix(".warnings.json"),
    }
    assert len(export_bases) == 1

    markdown = (workspace / output["md_path"]).read_text(encoding="utf-8")
    assert markdown.startswith("# Smoke Report")
    assert "Evidence Table" not in markdown
    assert "Claim-Evidence Matrix" not in markdown
    assert "Citations / Source IDs" not in markdown
    assert "Synthetic short citation" not in markdown
    assert "- [bad]" not in markdown
    assert markdown.count("## References") == 1
    assert "## References" in markdown
    assert "[s1]" in markdown
    assert "- [s1] Synthetic. https://example.com/s1" in markdown
    assert markdown.index("## References") > markdown.index("## Findings")
    assert (workspace / output["pdf_path"]).read_bytes().startswith(b"%PDF")
    assert (workspace / output["docx_path"]).read_bytes().startswith(b"PK")
    pdf_reader = PdfReader(str(workspace / output["pdf_path"]))
    uris: list[str] = []
    for page in pdf_reader.pages:
        for annotation_ref in page.get("/Annots") or []:
            annotation = annotation_ref.get_object()
            action = annotation.get("/A") or {}
            uri = action.get("/URI") if hasattr(action, "get") else None
            if uri:
                uris.append(str(uri))
    assert "https://example.com/s1" in uris
    manifest = json.loads((workspace / output["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["pdf_sha256"] == output["pdf_sha256"]
    assert manifest["docx_sha256"] == output["docx_sha256"]
    assert manifest["pdf_content_type"] == "application/pdf"
    assert (
        manifest["docx_content_type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert output["export_status"] == {"smoke": True}
    assert manifest["model_export_status"] == {"smoke": True}
    assert manifest["research_run_manifest"] == {"bundle_id": "deep-research", "status": "smoke"}

    child_workflows = {
        child.workflow_id
        for child in run_store.list_children(parent_run_id=run_id)
        if child.status == RunStatus.COMPLETED
    }
    assert "deep-research@0.1.8:deep-plan" in child_workflows
    assert "deep-research@0.1.8:deep-investigate" in child_workflows
    assert "deep-research@0.1.8:deep-review" in child_workflows
    assert "deep-research@0.1.8:deep-render" in child_workflows
