"""The gateway never caps an entity's output (operator 2026-10-02, 0.11.1).

No code default, no env knob, no per-request field: `max_output_tokens` never
rides the entity LLM kwargs, so the model works at its full capacity. A client
still sending the removed chat-open field is ignored with a one-line deprecation.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Dict

import pytest

import abstractgateway
from abstractgateway import entity_chat
from abstractgateway.routes import entities as entities_routes

_SRC = Path(abstractgateway.__file__).parent


def test_entity_sources_never_send_an_output_cap() -> None:
    for rel in ("entity_chat.py", "entities.py"):
        text = (_SRC / rel).read_text(encoding="utf-8")
        assert "max_output_tokens" not in text, rel
        assert "ENTITY_MAX_OUTPUT_TOKENS" not in text, rel
    assert not hasattr(entity_chat, "resolve_entity_output_cap")
    assert not hasattr(entity_chat, "ENTITY_MAX_OUTPUT_TOKENS_ENV")
    assert "max_output_tokens" not in inspect.signature(entity_chat.EntityChatHost.open).parameters


class _FakeChatHost:
    def __init__(self) -> None:
        self.kwargs: Dict[str, Any] = {}

    def open(self, name: str, **kwargs: Any) -> Dict[str, Any]:
        self.kwargs = dict(kwargs)
        return {"ok": True, "name": name}


def _no_visit_host() -> Any:
    raise RuntimeError("no visit host")


def test_chat_open_ignores_the_removed_field_with_a_deprecation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_MAX_OUTPUT_TOKENS", "64")
    host = _FakeChatHost()
    monkeypatch.setattr(entities_routes, "_chat_host", lambda: host)
    monkeypatch.setattr(entities_routes, "_visit_host", _no_visit_host)

    req = entities_routes.OpenChatRequest(max_output_tokens=50)
    out = asyncio.run(entities_routes.open_entity_chat("castor", req))
    assert "max_output_tokens" not in host.kwargs
    assert "ignored" in out["deprecation"]

    out = asyncio.run(entities_routes.open_entity_chat("castor", entities_routes.OpenChatRequest()))
    assert "deprecation" not in out and "max_output_tokens" not in host.kwargs
