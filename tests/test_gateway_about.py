"""GET /api/gateway/about (CONTRACTS §B) and the tray About rows."""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def test_about_is_public_and_holds_versions_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app

    with TestClient(app) as client:
        r = client.get("/api/gateway/about")  # no Authorization header
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"abstractframework", "abstractgateway", "packages"}
        assert body["abstractgateway"] == metadata.version("abstractgateway")
        assert body["packages"]["abstractcore"] == metadata.version("abstractcore")
        assert all(k.startswith("abstract") for k in body["packages"])
        text = r.text
        assert str(tmp_path) not in text and str(Path.home()) not in text and "/" not in "".join(body["packages"])
        # Everything else still needs a sign-in.
        assert client.get("/api/gateway/runs").status_code == 401


def test_tray_about_lines_carry_identity_and_gateway_rows() -> None:
    from abstractgateway.tray.app import TrayApp
    from abstractgateway.tray.client import Result

    fake = SimpleNamespace(
        version="0.4.4",
        client=SimpleNamespace(about=lambda: Result(ok=True, status=200, data={
            "abstractframework": "0.3.3", "abstractgateway": "0.4.4", "packages": {"abstractcore": "2.15.3"}})),
    )
    lines = TrayApp._identity_lines(fake)
    text = "\n".join(lines)
    assert "Application: AbstractGateway 0.4.4" in text and "Part of: AbstractFramework" in text
    assert "Report an issue:" in text and "Gateway framework: AbstractFramework 0.3.3" in text
    assert "Gateway package abstractcore: 2.15.3" in text

    down = SimpleNamespace(version="0.4.4", client=SimpleNamespace(about=lambda: Result(ok=False, status=0, error="refused")))
    assert any(l.startswith("Gateway: unavailable (refused") for l in TrayApp._identity_lines(down))
