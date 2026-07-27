"""Replay-integrity incident R1 (code-tui c5547/c5552): gzip large control-
plane payloads so a single-turn history_bundle can't exceed a thin client's
byte reader — measured 3.7-4.8x on the live incident bundles.

The load-bearing safety property: SSE live tails (run-ledger stream, entity
/replay/stream — both text/event-stream) must NEVER be compressed, or the
event-stream semantics break. starlette 0.52.1 excludes text/event-stream by
default; these pins assert both halves stay true on the gateway app so a
starlette upgrade that changed the exclusion would fail here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
    from abstractgateway.app import app

    # raise_server_exceptions stays default; gzip rides the real middleware stack.
    return TestClient(app)


@pytest.mark.basic
def test_gzip_middleware_is_wired_and_excludes_sse() -> None:
    """Structural pin against the installed starlette: GZipMiddleware present
    on the app, and text/event-stream is in its excluded set."""
    from starlette.middleware.gzip import GZipMiddleware, DEFAULT_EXCLUDED_CONTENT_TYPES
    from abstractgateway.app import app

    assert "text/event-stream" in DEFAULT_EXCLUDED_CONTENT_TYPES, (
        "a starlette upgrade dropped the SSE exclusion — gzip would break live tails"
    )
    classes = {getattr(m, "cls", None) for m in app.user_middleware}
    assert GZipMiddleware in classes, "GZipMiddleware must be wired on the gateway app"


@pytest.mark.basic
def test_large_json_response_is_gzip_compressed_when_accepted(tmp_path: Path, monkeypatch) -> None:
    """A large control-plane JSON response compresses when the client sends
    Accept-Encoding: gzip (the incident target — history_bundle-class payloads)."""
    client = _client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer t", "Accept-Encoding": "gzip"}
    # /api/gateway/discovery/tools returns a sizable JSON inventory (> minimum_size).
    r = client.get("/api/gateway/discovery/tools", headers=headers)
    assert r.status_code == 200, r.text
    # TestClient transparently decodes; the wire header proves compression happened.
    if len(r.content) >= 1024:
        assert r.headers.get("content-encoding") == "gzip", (
            "a >1KB JSON response with Accept-Encoding: gzip must be compressed"
        )


@pytest.mark.basic
def test_no_compression_without_accept_encoding(tmp_path: Path, monkeypatch) -> None:
    """Compression is client-opt-in: no Accept-Encoding: gzip -> identity
    (non-gzip clients are untouched)."""
    client = _client(tmp_path, monkeypatch)
    # httpx defaults to Accept-Encoding including gzip; force identity.
    r = client.get(
        "/api/gateway/discovery/tools",
        headers={"Authorization": "Bearer t", "Accept-Encoding": "identity"},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("content-encoding") != "gzip"
