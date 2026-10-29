"""The optional AbstractNote seam (abstractnote_seam.py): off without the package, on with it.

The full behaviour with AbstractNote installed (read API answers, isolation, tool binding) is
tested in the abstractnote repository (tests/test_gateway_router.py); here the gateway's own
guarantees: without `abstractnote` nothing changes and nothing raises (a broken package
included), the app catalog lists Note on 3006, a binding failure answers on every call instead
of taking the plane down, and with the package the API is mounted behind the gateway's sign-in,
the workflows pack once into a bundle the gateway's loader opens, and the auto-approval
widening names the note tools only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from abstractgateway import abstractnote_seam as seam

# These tests exercise the seam's workflow packing itself (conftest keeps it off elsewhere).
pytestmark = pytest.mark.note_workflows


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


class _BrokenPackage:
    """A meta-path finder that makes `abstractnote` importable but broken (any error but ImportError)."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "abstractnote" or fullname.startswith("abstractnote."):
            raise RuntimeError("abstractnote is installed but broken")
        return None


def test_a_broken_package_is_treated_as_absent_and_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path):
    for name in [n for n in sys.modules if n == "abstractnote" or n.startswith("abstractnote.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BrokenPackage()] + list(sys.meta_path))
    monkeypatch.setattr(seam, "_state", {})
    assert seam.abstractnote_available() is False
    assert seam.note_router() is None
    assert seam.note_auto_approve_names() == frozenset()
    assert seam.note_tool_map(data_root=tmp_path, artifact_store=object(), account="alice") == {}
    assert seam.note_bundle_paths(tmp_path) == []


def test_with_abstractnote_the_api_is_mounted_behind_the_gateway_sign_in(monkeypatch: pytest.MonkeyPatch, tmp_path):
    pytest.importorskip("abstractnote.gateway", reason="abstractnote is not installed")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "seam-test-token-0123456789")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app
    from abstractnote.gateway.router import close_notebooks

    try:
        assert TestClient(app).get("/api/gateway/note/status").status_code == 401
        signed_in = TestClient(app, headers={"Authorization": "Bearer seam-test-token-0123456789"})
        status = signed_in.get("/api/gateway/note/status")
        assert status.status_code == 200 and "counts" in status.json() and "notebook" not in status.json()
        assert signed_in.get("/api/gateway/note/zzz-unknown").status_code == 404
    finally:
        close_notebooks()


def test_with_abstractnote_the_workflows_pack_once_into_a_framework_bundle(tmp_path: Path):
    pytest.importorskip("abstractnote.flows.bundle", reason="abstractnote is not installed")
    from abstractruntime.workflow_bundle import open_workflow_bundle

    [path] = seam.note_bundle_paths(tmp_path)
    assert path.is_file() and path.name.startswith("abstractnote@") and path.suffix == ".flow"
    assert path.parent.parent.parent == tmp_path / "extensions"
    assert not path.parent.with_name(path.parent.name + ".tmp").exists()
    bundle = open_workflow_bundle(path)
    assert bundle.manifest.bundle_id == "abstractnote"
    stamp = path.stat().st_mtime_ns
    assert seam.note_bundle_paths(tmp_path) == [path] and path.stat().st_mtime_ns == stamp


def test_with_abstractnote_only_the_note_tools_are_auto_approved():
    pytest.importorskip("abstractnote.gateway.tools", reason="abstractnote is not installed")
    from abstractnote.gateway.tools import AUTO_APPROVE_TOOL_NAMES
    from abstractruntime.integrations.abstractcore.tool_executor import ToolApprovalPolicy

    names = seam.note_auto_approve_names()
    assert names == frozenset(AUTO_APPROVE_TOOL_NAMES) and "note_capture" in names
    policy = ToolApprovalPolicy()
    before = (set(policy.auto_approve_tools), set(policy.require_approval_tools))
    policy.auto_approve_tools |= set(names)  # what hosts/bundle_host.py does
    assert policy.requires_approval([{"name": "note_capture", "arguments": {}}]) is False
    assert policy.requires_approval([{"name": "write_file", "arguments": {}}]) is True
    assert policy.auto_approve_tools - names == before[0] and policy.require_approval_tools == before[1]
