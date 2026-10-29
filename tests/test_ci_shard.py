"""CI shards (scripts/ci_shard.py): every test file runs in exactly one shard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ci_shard", ROOT / "scripts" / "ci_shard.py")
ci_shard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_shard)


def test_every_file_lands_in_exactly_one_shard() -> None:
    files = ci_shard.test_files()
    assert "tests/test_ci_shard.py" in files and any(f.startswith("tests/e2e_events/") for f in files)
    for n in (1, 3, 4, 5):
        shards = ci_shard.plan(n, files, ci_shard.load_durations())
        flat = [f for s in shards for f in s]
        assert sorted(flat) == files and len(flat) == len(set(flat)), n


def test_balancing_puts_long_files_apart() -> None:
    files = ["a", "b", "c", "d"]
    shards = ci_shard.plan(2, files, {"a": 100, "b": 90, "c": 10, "d": 5})
    assert {frozenset(s) for s in shards} == {frozenset({"a", "d"}), frozenset({"b", "c"})}


def test_the_durations_file_names_real_test_files() -> None:
    known = set(ci_shard.load_durations())
    assert known, "ci/test_durations.json is missing: the shards would not be balanced"
    files = set(ci_shard.test_files())
    assert len(known & files) >= 0.9 * len(known), "ci/test_durations.json names files that no longer exist"
