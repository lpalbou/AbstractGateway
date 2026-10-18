"""Durable, session-local approval gates over the entity's own executor."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from abstractruntime import Effect, EffectType, Runtime, StepPlan, WorkflowSpec
from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState
from abstractruntime.core.runtime import EffectOutcome
from abstractruntime.storage.sqlite import SqliteDatabase, SqliteLedgerStore, SqliteRunStore

from abstractgateway.entity_session_tools import (decide, install_policy, normalize_policy,
                                                 require_owner, session_state, wrap_entity_tool_handler)
from abstractgateway.entity_visits import VisitRefused


OPTIONS = [{"name": "read_memory"}, {"name": "send_email"}]


class Host:
    def __init__(self, tmp_path, modes):
        self.calls = []
        self.grant = {"read_memory", "send_email"}
        self.db = SqliteDatabase(tmp_path / "visit.sqlite3")
        self.store = SqliteRunStore(self.db)
        self.ledger = SqliteLedgerStore(self.db)

        def execute(run, effect, next_node=None):
            rows = []
            for call in effect.payload["tool_calls"]:
                name = call["name"]
                if name not in self.grant:
                    rows.append({"name": name, "call_id": call["call_id"], "success": False,
                                 "output": None, "error": "Fresh grant refuses this tool.", "executed": False})
                else:
                    self.calls.append(name)
                    rows.append({"name": name, "call_id": call["call_id"], "success": True, "output": "ran"})
            return EffectOutcome.completed({"mode": "executed", "results": rows})

        handler = wrap_entity_tool_handler(execute)
        self.runtime = Runtime(run_store=self.store, ledger_store=self.ledger,
                               effect_handlers={EffectType.TOOL_CALLS: handler})

        def act(run, ctx):
            return StepPlan(node_id="act", effect=Effect(type=EffectType.TOOL_CALLS,
                payload={"tool_calls": [{"name": "send_email", "arguments": {}, "call_id": "email-1"},
                                         {"name": "read_memory", "arguments": {}, "call_id": "read-1"}], "act_seq": 1},
                result_key="tool_results"), next_node="park")

        def park(run, ctx):
            run.vars["_visit"]["history"].append({"role": "assistant", "content": "A new answer"})
            return StepPlan(node_id="park", effect=Effect(type=EffectType.WAIT_EVENT,
                payload={"wait_key": "visitor_input"}, result_key="visitor"), next_node="act")

        self.wf = WorkflowSpec(workflow_id="entity-visit@1", entry_node="act", nodes={"act": act, "park": park})
        initial = {"_visit": {"turn_n": 1, "history": [{"role": "assistant", "content": "Old answer"}]}}
        install_policy(initial, normalize_policy(modes, options=OPTIONS))
        self.run_id = self.runtime.start(workflow=self.wf, vars=initial, session_id="phone-session")
        self.er = SimpleNamespace(run_store=self.store, ledger_store=self.ledger, runtime=self.runtime)
        self.manifest = SimpleNamespace(slug="castor")
        self._registry = SimpleNamespace()

    def _load_visit(self, name, run_id, need_spec=True):
        if name != "castor" or run_id != self.run_id:
            raise VisitRefused(404, "Wrong home")
        return self.er, self.store.load(run_id), self.wf if need_spec else None, self.manifest

    def _refuse_if_paused(self, manifest, verb):
        pass

    def _acquire_lease(self, er):
        from abstractruntime.storage.lease import acquire_directory_lease

        return acquire_directory_lease(self.db.path.parent if hasattr(self.db, "path") else self.folder, holder="session-test")


@pytest.fixture
def host(tmp_path):
    value = Host(tmp_path, {"read_memory": "auto", "send_email": "approve"})
    value.folder = tmp_path
    yield value
    value.db.close()


def test_approval_wait_is_persisted_before_any_execution_and_approval_runs_once(host):
    state = host.runtime.tick(workflow=host.wf, run_id=host.run_id)
    assert state.status == RunStatus.WAITING and state.waiting.reason == WaitReason.USER
    assert host.calls == []
    persisted = SqliteRunStore(host.db).load(host.run_id)
    assert persisted.waiting.wait_key == state.waiting.wait_key
    view = session_state(host, "castor", host.run_id)
    assert view["reply"] is None and not view["turn_complete"]
    assert view["pending_wait"]["details"]["mode"] == "approval_required"
    assert "original_payload" not in view["pending_wait"]["details"]
    done = decide(host, "castor", host.run_id, wait_key=state.waiting.wait_key, approved=True)
    assert host.calls == ["send_email", "read_memory"]
    assert done["reply"] == "A new answer" and done["turn_complete"]
    with pytest.raises(VisitRefused) as stale:
        decide(host, "castor", host.run_id, wait_key=state.waiting.wait_key, approved=True)
    assert stale.value.status == 409
    assert len(host.calls) == 2


def test_deny_and_wrong_wait_never_execute(host):
    state = host.runtime.tick(workflow=host.wf, run_id=host.run_id)
    with pytest.raises(VisitRefused) as wrong:
        decide(host, "castor", host.run_id, wait_key="wrong", approved=True)
    assert wrong.value.status == 409 and host.calls == []
    decide(host, "castor", host.run_id, wait_key=state.waiting.wait_key, approved=False)
    rows = host.store.load(host.run_id).vars["tool_results"]["results"]
    assert host.calls == [] and all(r["blocked"] and not r["executed"] for r in rows)


def test_off_and_explicit_empty_policy_deny_including_email(tmp_path):
    for index, modes in enumerate([{}, {"send_email": "off", "read_memory": "auto"}]):
        directory = tmp_path / str(index)
        directory.mkdir()
        host = Host(directory, modes)
        host.runtime.tick(workflow=host.wf, run_id=host.run_id)
        assert host.calls == ([] if not modes else ["read_memory"])
        assert host.store.load(host.run_id).waiting.wait_key == "visitor_input"
        rows = host.store.load(host.run_id).vars["tool_results"]["results"]
        assert rows[0]["blocked"] and not rows[0]["executed"]
        host.db.close()


def test_fresh_grant_still_denies_after_approval(host):
    state = host.runtime.tick(workflow=host.wf, run_id=host.run_id)
    host.grant.remove("send_email")
    decide(host, "castor", host.run_id, wait_key=state.waiting.wait_key, approved=True)
    assert host.calls == ["read_memory"]
    assert host.store.load(host.run_id).vars["tool_results"]["results"][0]["success"] is False


def test_interrupted_decision_is_not_blindly_reexecuted(host):
    state = host.runtime.tick(workflow=host.wf, run_id=host.run_id)
    run = host.store.load(host.run_id)
    run.vars["_runtime"]["entity_session_tools"]["last_decision"] = {
        "wait_key": state.waiting.wait_key, "state": "executing"}
    host.store.save(run)
    with pytest.raises(VisitRefused) as interrupted:
        decide(host, "castor", host.run_id, wait_key=state.waiting.wait_key, approved=True)
    assert interrupted.value.code == "decision_outcome_unknown" and host.calls == []


def test_foreign_owner_or_home_cannot_approve(host):
    from abstractgateway.security.principal import GatewayPrincipal, reset_current_gateway_principal, set_current_gateway_principal

    state = host.runtime.tick(workflow=host.wf, run_id=host.run_id)
    token = set_current_gateway_principal(GatewayPrincipal(user_id="another-user"))
    try:
        with pytest.raises(VisitRefused) as foreign:
            decide(host, "castor", host.run_id, wait_key=state.waiting.wait_key, approved=True)
        assert foreign.value.status == 404
    finally:
        reset_current_gateway_principal(token)
    with pytest.raises(VisitRefused):
        decide(host, "other-home", host.run_id, wait_key=state.waiting.wait_key, approved=True)
    assert host.calls == []


def test_null_restores_defaults_and_malformed_policy_fails_closed():
    assert normalize_policy(None, options=OPTIONS)["modes"] == {"read_memory": "auto", "send_email": "auto"}
    for bad in [{"send_email": "ask"}, {"foreign_tool": "auto"}, [], {"read_memory": True}]:
        with pytest.raises(ValueError):
            normalize_policy(bad, options=OPTIONS)


def test_automation_defaults_import_actual_runtime_constant():
    from abstractruntime.session_history import HISTORY_REPLAY_MAX_TOKENS
    from abstractgateway.routes.session_tools import automation_defaults

    assert automation_defaults() == {"growing_max_tokens": HISTORY_REPLAY_MAX_TOKENS}
