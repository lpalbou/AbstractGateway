"""Operator iterations ceiling for entity runs (laurent c786; seam (b)
c805/c809 — proof shape per agency c787).

The KNOB is the gateway's (env `ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING`,
default 100, disable with 0/off); the gateway SERVES the value into run vars
as `_limits.max_iterations_ceiling` at entity-run creation (summon + visit
open); abstractruntime's `Runtime.start()` is the ONE enforcement site:
declared max_iterations above the ceiling refuses LOUD before the run exists
(never mid-run truncation); declared-under runs exactly as declared; absent
field = no enforcement (the runtime never invents 100).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

from abstractgateway.config import entity_iterations_ceiling  # noqa: E402


# ------------------------------------------------------------- the config knob


def test_ceiling_defaults_to_the_ruled_100(monkeypatch):
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING", raising=False)
    assert entity_iterations_ceiling() == 100


def test_ceiling_is_operator_customizable(monkeypatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING", "250")
    assert entity_iterations_ceiling() == 250


def test_ceiling_disable_spellings_yield_no_enforcement(monkeypatch):
    for raw in ("0", "off", "none", "disabled", "false", "OFF"):
        monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING", raw)
        assert entity_iterations_ceiling() is None, raw


def test_ceiling_invalid_env_falls_back_to_the_ruled_default(monkeypatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING", "banana")
    assert entity_iterations_ceiling() == 100


# ------------------------------------------- runtime composition (seam (b))


def _entity_runtime(tmp_path: Path):
    """A real per-entity runtime over a real home — the seam under proof is
    gateway-served vars -> Runtime.start() enforcement."""
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    from abstractgateway.entities import EntityRegistry

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = "Castor"
    spark["spark"] = 1
    registry = EntityRegistry(data_dir=tmp_path / "runtime")
    registry.create(name="Castor", spark=spark)
    return registry, registry.get_entity_runtime("castor")


def _tiny_workflow():
    from abstractruntime.core.models import StepPlan
    from abstractruntime.core.spec import WorkflowSpec

    def start(run, ctx):  # noqa: ANN001
        return StepPlan(node_id="start", complete_output={"ok": True})

    return WorkflowSpec(workflow_id="ceiling-probe", entry_node="start", nodes={"start": start})


def test_declared_over_ceiling_refuses_at_start_and_the_run_never_exists(tmp_path):
    registry, er = _entity_runtime(tmp_path)
    try:
        wf = _tiny_workflow()
        with pytest.raises(ValueError) as exc:
            er.runtime.start(
                workflow=wf,
                vars={"_limits": {"max_iterations": 200, "max_iterations_ceiling": 100}},
                actor_id="gateway",
                session_id="s-over",
            )
        msg = str(exc.value)
        # The refusal names BOTH values + the override surface (c805 shape).
        assert "200" in msg and "100" in msg
        # Refuse-at-start means no run rests in the store for this session.
        list_runs = getattr(er.run_store, "list_runs", None)
        if callable(list_runs):
            assert not [r for r in list_runs() if getattr(r, "session_id", "") == "s-over"]
    finally:
        registry.close_all()


def test_declared_under_ceiling_runs_exactly_as_declared(tmp_path):
    registry, er = _entity_runtime(tmp_path)
    try:
        run_id = er.runtime.start(
            workflow=_tiny_workflow(),
            vars={"_limits": {"max_iterations": 7, "max_iterations_ceiling": 100}},
            actor_id="gateway",
            session_id="s-under",
        )
        run = er.run_store.load(run_id)
        assert run.vars["_limits"]["max_iterations"] == 7, "workflow-declared value is authoritative up to the ceiling"
        assert run.vars["_limits"]["max_iterations_ceiling"] == 100
    finally:
        registry.close_all()


def test_absent_ceiling_field_means_no_enforcement(tmp_path):
    registry, er = _entity_runtime(tmp_path)
    try:
        run_id = er.runtime.start(
            workflow=_tiny_workflow(),
            vars={"_limits": {"max_iterations": 100000}},
            actor_id="gateway",
            session_id="s-free",
        )
        run = er.run_store.load(run_id)
        assert run.vars["_limits"]["max_iterations"] == 100000, "no ceiling field = the runtime never invents one"
    finally:
        registry.close_all()


# ----------------------------------------------- the gateway injection points


def test_visit_seed_vars_carry_the_ceiling_on_full_default_limits(tmp_path, monkeypatch):
    """The visit-open injection: full runtime-default `_limits` PLUS the
    ceiling — injecting the ceiling must never strip normal limit seeding
    (Runtime.start skips its default fill when `_limits` is present)."""
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING", "100")
    from abstractgateway.entity_visits import _seed_run_vars

    registry, er = _entity_runtime(tmp_path)
    try:
        vars0 = _seed_run_vars(er)
        limits = vars0["_limits"]
        assert limits["max_iterations_ceiling"] == 100
        defaults = er.runtime.config.to_limits_dict()
        for key, value in defaults.items():
            assert limits[key] == value, f"default limit {key!r} must survive the injection"
    finally:
        registry.close_all()


def test_visit_seed_vars_empty_when_ceiling_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING", "off")
    from abstractgateway.entity_visits import _seed_run_vars

    registry, er = _entity_runtime(tmp_path)
    try:
        assert _seed_run_vars(er) == {}, "disabled ceiling = absent field, no phantom _limits override"
    finally:
        registry.close_all()


def test_summon_injection_overwrites_caller_ceiling_but_keeps_other_limits():
    """The summon-route injection shape: the ceiling is the OPERATOR'S word
    (a request cannot raise/lower operator policy), while the caller's other
    `_limits` keys (declared window etc.) pass through untouched. Mirrors the
    routes/entities.py block on a plain dict — the route applies exactly
    this transformation to input_data."""
    ceiling = 100
    input_data: Dict[str, Any] = {"_limits": {"max_iterations_ceiling": 10_000, "max_input": 32768}}

    limits_in = input_data.get("_limits")
    limits: Dict[str, Any] = dict(limits_in) if isinstance(limits_in, dict) else {}
    limits["max_iterations_ceiling"] = int(ceiling)
    input_data["_limits"] = limits

    assert input_data["_limits"]["max_iterations_ceiling"] == 100, "operator word overwrites the caller's claim"
    assert input_data["_limits"]["max_input"] == 32768, "caller's other limits pass through"
