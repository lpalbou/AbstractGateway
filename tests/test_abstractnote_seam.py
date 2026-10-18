"""The optional AbstractNote seam (abstractnote_seam.py): off without the package, on with it.

The full behaviour with AbstractNote installed (read API, isolation, tool binding, approvals)
is tested in the abstractnote repository (tests/test_gateway_router.py); here the gateway's own
guarantees: without `abstractnote` nothing changes, the app catalog lists Note on 3006, and a
binding failure answers on every call instead of taking the plane down.
"""

from __future__ import annotations

import sys

import pytest

from abstractgateway import abstractnote_seam as seam


@pytest.fixture()
def without_abstractnote(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "abstractnote.gateway", None)  # import raises ImportError
    monkeypatch.setattr(seam, "_state", {})
    yield
    seam._state.clear()


def test_without_abstractnote_the_seam_is_inert(without_abstractnote, tmp_path):
    assert seam.abstractnote_available() is False
    assert seam.note_tool_map(data_root=tmp_path, artifact_store=object(), account="alice") == {}
    assert seam.note_auto_approve_names() == frozenset()
    assert seam.note_router() is None
    assert seam.note_bundle_paths(tmp_path) == []
    assert not (tmp_path / "abstractnote").exists() and not (tmp_path / "extensions").exists()


def test_the_import_is_attempted_once(monkeypatch: pytest.MonkeyPatch, without_abstractnote):
    calls = []
    real = seam.logger.info
    monkeypatch.setattr(seam.logger, "info", lambda *a, **k: (calls.append(a), real(*a, **k)))
    for _ in range(3):
        seam.abstractnote_available()
    assert len(calls) == 1


def test_a_binding_failure_answers_on_every_call(monkeypatch: pytest.MonkeyPatch, tmp_path):
    pytest.importorskip("abstractnote.gateway.tools", reason="abstractnote is not installed")
    import abstractnote.gateway.tools as note_tools

    monkeypatch.setattr(seam, "_state", {"available": True})

    def boom(*_a, **_k):
        raise ValueError("This notebook is pinned to another embedding model.")

    monkeypatch.setattr(note_tools, "bound_tool_map", boom)
    tools = seam.note_tool_map(data_root=tmp_path, artifact_store=object(), account="alice")
    assert set(tools) == set(note_tools.AUTO_APPROVE_TOOL_NAMES)
    with pytest.raises(ValueError, match="Your notebook is not available: This notebook is pinned"):
        tools["note_capture"](text="x")


def test_the_app_catalog_lists_note_on_3006():
    from abstractgateway.apps_manager import APP_BY_ID, FLAG_CONFIGURED_APPS, STACK_PORT
    from abstractgateway.tray.apps import WEB_APPS

    row = APP_BY_ID["note"]
    assert (row.name, row.package, row.default_port, row.cookie_prefix, row.gateway_url_env, row.html_title) == (
        "Note", "@abstractframework/note", 3006, "abstractnote", "ABSTRACTNOTE_GATEWAY_URL", "AbstractNote")
    assert STACK_PORT["note"] == 3006 and "note" in FLAG_CONFIGURED_APPS
    assert ("note", "Note", "@abstractframework/note", "abstractnote-web", 3006, "ABSTRACTNOTE_GATEWAY_URL") in WEB_APPS
