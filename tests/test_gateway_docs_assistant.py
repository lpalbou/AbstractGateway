"""Docs assistant surface (uic c1648 slice b): the corpus route the console
drawer grounds on + the .af-topbar/.af-drawer public markup in the served
console HTML.

The docs-qa CONTRACT forbids corpus guessing — the corpus route is how the
console (whose app IS the gateway) supplies its own llms.txt. Resolution is
env-override-first and HONEST: an operator-set path that does not exist is a
404 naming the checked candidates, never a silent fallback to another file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

_TOKEN = "docs-assistant-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_corpus_env_override_serves_the_named_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    corpus = tmp_path / "llms.txt"
    corpus.write_text("# MyApp docs\ncatalog versions are immutable\n", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_DOCS_CORPUS", str(corpus))
    with _client() as client:
        r = client.get("/api/gateway/docs/corpus")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "env:ABSTRACTGATEWAY_DOCS_CORPUS"
        assert "immutable" in body["text"]
        assert body["chars"] == len(body["text"])
        assert body["app"] == "AbstractGateway"


def test_corpus_env_override_missing_is_an_honest_404_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Operator intent is authoritative: a set-but-missing override must NOT
    silently fall back to the repo corpus (which exists in dev checkouts)."""
    monkeypatch.setenv("ABSTRACTGATEWAY_DOCS_CORPUS", str(tmp_path / "nope.txt"))
    with _client() as client:
        r = client.get("/api/gateway/docs/corpus")
        assert r.status_code == 404, r.text
        assert "ABSTRACTGATEWAY_DOCS_CORPUS" in r.json()["detail"]


def test_corpus_requires_authentication(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    corpus = tmp_path / "llms.txt"
    corpus.write_text("docs", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_DOCS_CORPUS", str(corpus))
    from abstractgateway.app import app

    with TestClient(app) as anon:
        r = anon.get("/api/gateway/docs/corpus")
        assert r.status_code in (401, 403), r.text


def test_corpus_candidate_order_repo_then_packaged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an override: dev repo llms.txt first, wheel-packaged copy second.
    With an override: ONLY the override is offered (authoritative)."""
    monkeypatch.delenv("ABSTRACTGATEWAY_DOCS_CORPUS", raising=False)
    from abstractgateway.routes.gateway import _docs_corpus_candidates

    labels = [label for label, _ in _docs_corpus_candidates()]
    assert labels == ["repo:llms.txt", "packaged:assets/llms.txt"]

    monkeypatch.setenv("ABSTRACTGATEWAY_DOCS_CORPUS", "/tmp/x.txt")
    labels = [label for label, _ in _docs_corpus_candidates()]
    assert labels == ["env:ABSTRACTGATEWAY_DOCS_CORPUS"]


def test_corpus_is_packaged_in_the_wheel_config() -> None:
    """The packaged candidate is only honest if the wheel actually carries the
    corpus — pin the pyproject force-include so removing it turns this red."""
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert '"llms.txt" = "abstractgateway/assets/llms.txt"' in text


def test_console_mounts_the_kit_docs_assistant_island() -> None:
    """Round 8 (R8.3): the console's Docs assistant IS the kit's
    DocsAssistantDrawer (panel-chat) mounted from the islands bundle — the
    hand-built drawer (its own textarea, "New conversation" text button and
    docs-qa poller) is gone. The static top-bar fallback keeps appearance ->
    connection pill; the docs button lives in the island cluster."""
    from abstractgateway.console import gateway_console_html
    from abstractgateway.console_islands import ISLANDS_CSS, ISLANDS_JS

    html = gateway_console_html()
    assert 'class="status af-topbar"' in html
    assert html.index('id="open-appearance"') < html.index('id="sign-out"')
    assert "af-topbar__pill" in html and "af-topbar__pill-label" in html
    assert 'id="af-docs-assistant-root"' in html
    assert "lib.mountDocsAssistant(" in html
    assert 'const DOCS_ASSISTANT_SOURCE = { app: "gateway", name: "AbstractGateway" };' in html
    for gone in ('id="assistant-drawer"', 'id="assistant-input"', 'id="assistant-form"', 'id="open-assistant"', ">New conversation</button>", "ASSISTANT_BUNDLE"):
        assert gone not in html, gone
    # The vendored bundle carries the kit component and its CSS.
    assert "mountDocsAssistant" in ISLANDS_JS and "pc-docs-assistant" in ISLANDS_JS and '"docs-qa"' in ISLANDS_JS
    assert ".pc-docs-assistant__footer" in ISLANDS_CSS
    # The console's hand-mapped .pc-chat-item rules never restyle the kit drawer.
    assert ".pc-chat-item:where(:not(.af-sandbox-chat *, .pc-docs-assistant *))" in html


# --- Each app's corpus: GET /docs/corpus?app=<id> (round 8, R8.3) -------------


def _serve_llms_txt(text: str, content_type: str = "text/plain; charset=utf-8"):
    """A real loopback app server answering GET /llms.txt."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = text.encode("utf-8")
            if self.path != "/llms.txt":
                body = b"<!doctype html><title>shell</title>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            else:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _pin_port(monkeypatch: pytest.MonkeyPatch, port):
    from abstractgateway import apps_manager

    monkeypatch.setattr(apps_manager.AppsManager, "serving_port", lambda self, spec: port if spec.id == "code" else None)


def test_app_corpus_is_the_running_apps_own_llms_txt(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _serve_llms_txt("# AbstractCode\n\n> Browser coding assistant.\n")
    try:
        _pin_port(monkeypatch, srv.server_address[1])
        with _client() as client:
            r = client.get("/api/gateway/docs/corpus", params={"app": "code"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["app"] == "AbstractCode"
            assert body["source"] == "app:code:llms.txt"
            assert body["text"].startswith("# AbstractCode")
            assert body["chars"] == len(body["text"])
    finally:
        srv.shutdown()


def test_app_corpus_refuses_an_html_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """An app without llms.txt answers its SPA shell for unknown paths: never a corpus."""
    srv = _serve_llms_txt("<!doctype html>", content_type="text/html")
    try:
        _pin_port(monkeypatch, srv.server_address[1])
        with _client() as client:
            r = client.get("/api/gateway/docs/corpus", params={"app": "code"})
            assert r.status_code == 404, r.text
            assert "AbstractCode on port" in r.json()["detail"] and "does not serve its documentation (llms.txt)" in r.json()["detail"]
    finally:
        srv.shutdown()


def test_app_corpus_when_the_app_is_not_running_or_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_port(monkeypatch, None)
    with _client() as client:
        r = client.get("/api/gateway/docs/corpus", params={"app": "code"})
        assert r.status_code == 404
        assert r.json()["detail"].startswith("AbstractCode is not running on this gateway")
        r = client.get("/api/gateway/docs/corpus", params={"app": "nope"})
        assert r.status_code == 404 and "Unknown app 'nope'" in r.json()["detail"]


def test_app_gateway_is_the_gateways_own_corpus(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    corpus = tmp_path / "llms.txt"
    corpus.write_text("# gateway docs\n", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_DOCS_CORPUS", str(corpus))
    with _client() as client:
        r = client.get("/api/gateway/docs/corpus", params={"app": "gateway"})
        assert r.status_code == 200 and r.json()["app"] == "AbstractGateway" and r.json()["text"] == "# gateway docs\n"


def test_vendored_pc_chat_class_set_is_pinned() -> None:
    """uic's drift belt (c2171): the console vendors panel-chat's .pc-chat-item
    recipe — a kit-side class rename must fail LOUD here instead of leaving the
    console half-styled. panel_chat.css is the source of truth; this pins the
    exact class family the console renders + styles."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    for cls in (
        ".pc-chat-item",
        ".pc-chat-item--user",
        ".pc-chat-item--assistant",
        ".pc-chat-item--status",
        ".pc-chat-item--error",
    ):
        assert cls in html, f"vendored pc-chat class missing from console CSS: {cls}"
    # The renderers speak the same vocabulary (sandbox + entity chat).
    assert "pc-chat-item pc-chat-item--" in html
    assert "pc-chat-thread" in html
