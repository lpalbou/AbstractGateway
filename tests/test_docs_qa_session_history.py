"""docs-qa 0.1.1: conversation history comes from the run's SESSION, never from the caller.

ADR-0026 + operator ruling 2026-09-28: no message-count or character caps;
replayed history is the newest whole turns up to 50,000 tokens, recorded.
0.1.0 took a client `history` array and kept its last 12 messages in its own
code node. 0.1.1 takes the question as `prompt`; each question starts in the
conversation's session with `use_session_history`, the gateway replays the
earlier turns into `context.messages` through the runtime's history window,
and the LLM node includes them (`use_context`). The docs ride the system
prompt, so replayed user turns are the questions, never the docs.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.basic

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "flows" / "bundles" / "docs-qa@0.1.2.flow"
SESSION = "gateway-docs-assistant:test"
DOCS = "# AbstractGateway\n\n## Providers\nAdd a provider on the Providers screen.\n"


def _flow() -> Dict[str, Any]:
    with zipfile.ZipFile(BUNDLE) as zf:
        return json.loads(zf.read("flows/docsqa001.json"))


def test_bundle_contract_has_no_history_input_and_no_turn_cap() -> None:
    flow = _flow()
    start = next(n for n in flow["nodes"] if n["id"] == "start")
    outputs = {p["id"] for p in start["data"]["outputs"]}
    assert "prompt" in outputs and "history" not in outputs and "question" not in outputs
    compose = next(n for n in flow["nodes"] if n["id"] == "compose")
    assert "history" not in compose["data"]["code"] and "[-12:]" not in compose["data"]["code"]
    llm = next(n for n in flow["nodes"] if n["id"] == "llm")
    assert llm["data"]["effectConfig"]["use_context"] is True
    with zipfile.ZipFile(BUNDLE) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["bundle_version"] == "0.1.2"
    assert "history" not in manifest["metadata"]["contract"]["inputs"]


def test_the_builder_reproduces_the_shipped_bundle(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_docs_qa_bundle", ROOT / "scripts" / "build_docs_qa_bundle.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    built = mod.build_bundle("0.1.2", tmp_path)
    with zipfile.ZipFile(built) as a, zipfile.ZipFile(BUNDLE) as b:
        fa, fb = json.loads(a.read("flows/docsqa001.json")), json.loads(b.read("flows/docsqa001.json"))
    for f in (fa, fb):
        f.pop("created_at"), f.pop("updated_at")
    assert fa == fb


def _tick_until_terminal(host, run_store, run_id: str, data_dir: Path):
    from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
    from abstractruntime.core.models import RunStatus

    runner = GatewayRunner(base_dir=data_dir, host=host, config=GatewayRunnerConfig(tick_max_steps=50, tick_workers=1), enable=False)
    for _ in range(20):
        run = run_store.load(run_id)
        if run is not None and run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return run
        runner._tick_run(run_id)
    raise AssertionError(f"run {run_id} did not finish: {run_store.load(run_id)}")


def test_second_question_replays_the_first_turn_through_the_session_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractgateway.service import run_summary
    from abstractruntime.core.models import EffectType, RunStatus
    from abstractruntime.core.runtime import EffectOutcome, Runtime
    from abstractruntime.integrations.abstractcore import factory as ac_factory
    from abstractruntime.storage.artifacts import InMemoryArtifactStore
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    bundles_dir = tmp_path / "bundles"
    data_dir = tmp_path / "data"
    bundles_dir.mkdir()
    shutil.copy2(BUNDLE, bundles_dir / BUNDLE.name)
    calls: List[Dict[str, Any]] = []

    def _fake_create_local_runtime(**kwargs):
        def _llm_stub(run, effect, default_next_node):
            del run, default_next_node
            calls.append(dict(effect.payload or {}))
            return EffectOutcome.completed({"content": f"answer-{len(calls)}", "tool_calls": []})

        return Runtime(
            run_store=kwargs["run_store"],
            ledger_store=kwargs["ledger_store"],
            artifact_store=kwargs.get("artifact_store") or InMemoryArtifactStore(),
            effect_handlers={EffectType.LLM_CALL: _llm_stub},
        )

    monkeypatch.setattr(ac_factory, "create_local_runtime", _fake_create_local_runtime)
    run_store = InMemoryRunStore()
    host = WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=data_dir,
        run_store=run_store,
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )

    def ask(question: str):
        run_id = host.start_run(
            flow_id="docsqa001",
            bundle_id="docs-qa",
            input_data={"prompt": question, "docs": DOCS, "app": "AbstractGateway", "use_session_history": True},
            session_id=SESSION,
        )
        run = _tick_until_terminal(host, run_store, run_id, data_dir)
        assert run.status == RunStatus.COMPLETED, run.error
        return run

    first = ask("how do I add a provider?")
    assert first.output["response"] == "answer-1"
    second = ask("and remove one?")
    assert second.output["response"] == "answer-2"

    sent = calls[-1]["messages"]
    # The runtime stamps a <runtime_metadata> envelope on user turns (grounding,
    # carried verbatim on replay); compare what the human typed.
    convo = [(m["role"], m["content"].split("</runtime_metadata>\n")[-1]) for m in sent if m.get("role") != "system"]
    assert convo == [
        ("user", "how do I add a provider?"),
        ("assistant", "answer-1"),
        ("user", "and remove one?"),
    ]
    systems = [m["content"] for m in sent if m.get("role") == "system"]
    assert len(systems) == 1 and "## Providers" in systems[0]
    # The docs are in the system prompt once, never repeated in replayed turns.
    assert all("## Providers" not in c for _r, c in convo)

    receipt = run_summary(second)["session_history"]
    assert receipt["replayed_messages"] == 2 and receipt["dropped_messages"] == 0
    assert receipt["max_tokens"] == 50_000


def test_basic_agent_with_an_empty_tools_list_offers_and_runs_no_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Observer's docs assistant runs basic-agent with `tools: []` so a docs
    question can never write files or run commands. Pin the gateway-side
    semantics it relies on: an explicit empty list is "no tools", never
    "the registry defaults"."""
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractruntime.core.models import EffectType, RunStatus
    from abstractruntime.core.runtime import EffectOutcome, Runtime
    from abstractruntime.integrations.abstractcore import factory as ac_factory
    from abstractruntime.storage.artifacts import InMemoryArtifactStore
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    bundles_dir = tmp_path / "bundles"
    data_dir = tmp_path / "data"
    bundles_dir.mkdir()
    shutil.copy2(ROOT / "flows" / "bundles" / "basic-agent@0.0.5.flow", bundles_dir / "basic-agent@0.0.5.flow")
    calls: List[Dict[str, Any]] = []

    def _fake_create_local_runtime(**kwargs):
        def _llm_stub(run, effect, default_next_node):
            del run, default_next_node
            calls.append(dict(effect.payload or {}))
            return EffectOutcome.completed({"content": "Open the Observe page.", "tool_calls": []})

        return Runtime(
            run_store=kwargs["run_store"],
            ledger_store=kwargs["ledger_store"],
            artifact_store=kwargs.get("artifact_store") or InMemoryArtifactStore(),
            effect_handlers={EffectType.LLM_CALL: _llm_stub},
        )

    monkeypatch.setattr(ac_factory, "create_local_runtime", _fake_create_local_runtime)
    run_store = InMemoryRunStore()
    host = WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=data_dir,
        run_store=run_store,
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )
    run_id = host.start_run(
        flow_id="81795ea9",
        bundle_id="basic-agent",
        input_data={
            "prompt": "why did my run fail?", "system": "docs", "tools": [], "use_session_history": True, "use_context": True,
            "provider": "stub", "model": "stub-model",
        },
        session_id="observer-docs-assistant:test",
    )
    from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig

    runner = GatewayRunner(base_dir=data_dir, host=host, config=GatewayRunnerConfig(tick_max_steps=50, tick_workers=1), enable=False)
    for _ in range(30):
        root = run_store.load(run_id)
        if root is not None and root.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            break
        for r in run_store.list_runs(status=RunStatus.RUNNING, limit=100):
            runner._tick_run(r.run_id)
    assert run_store.load(run_id).status == RunStatus.COMPLETED
    assert calls, "the agent made its model call"
    assert all(not c.get("tools") for c in calls), [c.get("tools") for c in calls]
    agents = [r for r in run_store.list_runs(limit=100) if "allowed_tools" in (r.vars.get("_runtime") or {})]
    assert agents and all(r.vars["_runtime"]["allowed_tools"] == [] for r in agents)
