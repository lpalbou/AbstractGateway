"""`serve --run-index trusted|verify` (gateway load 2026-10-26): verify (shadow) is the default of this
release, the store is built with `trust_index` accordingly, and the health body says the mode."""

from __future__ import annotations

import pytest

from abstractgateway import stores


def test_verify_is_the_default_and_the_store_follows_the_mode(tmp_path):
    from abstractruntime.storage.json_files import JsonFileRunStore

    stores.set_run_index_mode("verify")
    assert stores.run_index_mode() == "verify"
    built = stores.build_file_stores(base_dir=tmp_path)
    inner = getattr(built.run_store, "inner", None) or getattr(built.run_store, "_inner", None) or getattr(built.run_store, "store", None)
    assert isinstance(inner, JsonFileRunStore)
    assert inner._trust_index is False
    stores.set_run_index_mode("trusted")
    built2 = stores.build_file_stores(base_dir=tmp_path / "b")
    inner2 = getattr(built2.run_store, "inner", None) or getattr(built2.run_store, "_inner", None) or getattr(built2.run_store, "store", None)
    assert inner2._trust_index is True
    stores.set_run_index_mode("verify")


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        stores.set_run_index_mode("maybe")
    assert stores.run_index_mode() in ("verify", "trusted")
