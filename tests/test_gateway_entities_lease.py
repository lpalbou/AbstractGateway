"""Per-home lease, gateway sites (plan item 1, GW-A — phase 1 safety rails).

The primitive lives in `abstractruntime.storage.lease` (one contract, one
implementation — the loop process cannot import the gateway; re-homed from
identity.lease per the signed renaming doc, approved c398); these tests
pin the GATEWAY's two acquisition windows:

- the hosted VISIT (`EntityChatHost.open` -> close): holder="visit-host",
  held for the whole visit, released at close (wake write included);
  an open against a home that already has a writer refuses 409 naming the
  holder; the auto-yield negotiation stays ABOVE the lease.
- the DREAM window (`set_state(dream=True)`): holder="dream"; a held home
  SKIPS the pass with an honest #FALLBACK (sleep is never blocked; the
  pass is idempotent — same wording rule as the loop's own dream window).
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.storage.lease")

_TOKEN = "entity-lease-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    # Operator's substrate choice for this suite (scripted LLM is patched in).
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "test-model")


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class _ScriptedLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)

    def generate(self, **kwargs):
        text = self._replies.pop(0) if self._replies else "Nothing more."
        return self._Reply(text)


def _install_scripted_llm(monkeypatch: pytest.MonkeyPatch, replies) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM(replies))


def _home_dir(client: TestClient, name: str = "Castor"):
    from pathlib import Path

    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    return Path(svc.config.data_dir) / "entities" / name.lower()


def test_visit_holds_the_lease_and_releases_at_close(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.storage.lease import DirectoryLeaseHeld, acquire_directory_lease, read_directory_lease

    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection: a good visit."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir(client)

        opened = client.post("/api/gateway/entities/Castor/chat/open", json={})
        assert opened.status_code == 200, opened.text
        chat_id = opened.json()["chat_id"]

        # The visit IS the writer: lease held, holder named, second writer refused.
        state = read_directory_lease(home)
        assert state is not None and state["held"] is True
        assert state.get("holder") == "visit-host"
        with pytest.raises(DirectoryLeaseHeld, match="already has a writer"):
            acquire_directory_lease(home, holder="maintenance")

        closed = client.post(f"/api/gateway/entities/Castor/chat/{chat_id}/close", json={})
        assert closed.status_code == 200, closed.text

        # Window over: home reacquirable (stale metadata inert, probe honest).
        after = read_directory_lease(home)
        assert after is not None and after["held"] is False
        lease = acquire_directory_lease(home, holder="maintenance")
        lease.release()


def test_visit_open_refuses_while_home_has_a_writer(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.storage.lease import acquire_directory_lease

    _install_scripted_llm(monkeypatch, ["Hello."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir(client)

        incumbent = acquire_directory_lease(home, holder="maintenance")
        try:
            refused = client.post("/api/gateway/entities/Castor/chat/open", json={})
            assert refused.status_code == 409, refused.text
            detail = refused.json()["detail"]
            assert "already has a writer" in detail
            assert "maintenance" in detail  # the refusal names the holder
        finally:
            incumbent.release()

        # The moment the writer leaves, the same open succeeds.
        opened = client.post("/api/gateway/entities/Castor/chat/open", json={})
        assert opened.status_code == 200, opened.text
        client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close", json={})


def test_dream_pass_skips_when_home_held_and_runs_when_free(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.storage.lease import acquire_directory_lease, read_directory_lease

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir(client)

        # Held home: sleep succeeds, the pass is SKIPPED with the honest label.
        incumbent = acquire_directory_lease(home, holder="maintenance")
        try:
            slept = client.post(
                "/api/gateway/entities/Castor/state",
                json={"state": "asleep", "reason": "test night", "dream": True},
            )
            assert slept.status_code == 200, slept.text
            dream = slept.json()["dream"]
            assert dream["skipped"] is True
            assert "#FALLBACK" in dream["warning"]
            assert "idempotent" in dream["warning"]
        finally:
            incumbent.release()

        woke = client.post("/api/gateway/entities/Castor/state", json={"state": "awake", "reason": "morning"})
        assert woke.status_code == 200, woke.text

        # Free home: the pass runs (under its own lease) and the window closes.
        slept2 = client.post(
            "/api/gateway/entities/Castor/state",
            json={"state": "asleep", "reason": "second night", "dream": True},
        )
        assert slept2.status_code == 200, slept2.text
        dream2 = slept2.json()["dream"]
        assert dream2 is not None and not dream2.get("skipped")
        after = read_directory_lease(home)
        assert after is None or after["held"] is False
