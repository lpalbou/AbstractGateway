"""The fresh-install iteration default (operator decision D2, 2026-11-09: "50 everywhere"): a fresh
data dir stores agents.max_iterations = DEFAULT_MAX_ITERATIONS once, through the console's own door;
an install whose first run is done, a stored value, or a later clear is never touched. Scratch data
dirs only."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.basic


def test_a_fresh_data_dir_stores_50_once_and_a_clear_stays_cleared(tmp_path):
    from abstractruntime import DEFAULT_MAX_ITERATIONS

    from abstractgateway.runtime_config import (
        resolve_run_iteration_limit,
        seed_fresh_install_defaults,
        write_runtime_config,
    )

    data_dir = tmp_path / "fresh"
    assert resolve_run_iteration_limit(None, data_dir) == {"max_iterations": DEFAULT_MAX_ITERATIONS, "source": "builtin"}
    assert seed_fresh_install_defaults(data_dir) == ["agents.max_iterations"]
    assert resolve_run_iteration_limit(None, data_dir) == {"max_iterations": DEFAULT_MAX_ITERATIONS,
                                                           "source": "gateway_default"}
    stored = json.loads((data_dir / "config" / "runtime_config.json").read_text(encoding="utf-8"))
    assert stored["agents"]["max_iterations"] == DEFAULT_MAX_ITERATIONS
    assert stored["seeded_defaults"]["agents.max_iterations"]["value"] == DEFAULT_MAX_ITERATIONS
    assert seed_fresh_install_defaults(data_dir) == []  # once
    # The operator clears it: the seed never comes back.
    write_runtime_config(data_dir, {"agents.max_iterations": None}, actor="person:admin")
    assert seed_fresh_install_defaults(data_dir) == []
    assert resolve_run_iteration_limit(None, data_dir)["source"] == "builtin"


def test_an_existing_install_or_a_stored_value_is_never_touched(tmp_path):
    from abstractgateway.first_run import mark_first_run_complete
    from abstractgateway.runtime_config import resolve_run_iteration_limit, seed_fresh_install_defaults, write_runtime_config

    done = tmp_path / "done"
    mark_first_run_complete(done, by="person:admin")
    assert seed_fresh_install_defaults(done) == []
    assert resolve_run_iteration_limit(None, done)["source"] == "builtin"

    chosen = tmp_path / "chosen"
    write_runtime_config(chosen, {"agents.max_iterations": 30}, actor="person:admin")
    assert seed_fresh_install_defaults(chosen) == []
    assert resolve_run_iteration_limit(None, chosen) == {"max_iterations": 30, "source": "gateway_default"}
