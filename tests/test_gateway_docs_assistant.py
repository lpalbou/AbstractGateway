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


def test_console_renders_topbar_and_assistant_drawer_markup() -> None:
    """The console adopts abstractuic's CSS public API (.af-topbar/.af-drawer)
    with the contract-enforced order: assistant -> appearance -> extras ->
    connection pill rightmost (unified-top-bar behavior statement 1)."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    # Top bar cluster + enforced order.
    assert 'class="status af-topbar"' in html
    assistant_pos = html.index('id="open-assistant"')
    appearance_pos = html.index('id="open-appearance"')
    pill_pos = html.index('id="sign-out"')
    assert assistant_pos < appearance_pos < pill_pos
    assert "af-topbar__pill" in html and "af-topbar__pill-label" in html
    # Drawer public markup + console assistant internals.
    for needle in (
        'id="assistant-drawer"',
        "af-drawer__header",
        "af-drawer__body",
        'id="assistant-messages"',
        'id="assistant-input"',
        'id="assistant-form"',
    ):
        assert needle in html, needle
    # The drawer transport rides the published docs-qa catalog bundle.
    assert '"docs-qa"' in html and '"docsqa001"' in html
    assert "/api/gateway/docs/corpus" in html


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
