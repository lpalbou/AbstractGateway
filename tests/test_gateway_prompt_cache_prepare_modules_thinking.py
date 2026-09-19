"""Regression pin (2026-09-17): `/prompt_cache/prepare_modules` carries `thinking`.

`thinking` is part of a prepared prefix's identity: for models that render their effort
level at the head of the system block (Qwen3.8), a prefix planned under a different
request shares three tokens with the prompt generate() sends. The request model had no
such field and pydantic drops unknown keys silently, so a host that named the level got a
200 and a prefix that could never match.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.test_gateway_prompt_cache_endpoints import _make_client, _ProtocolOnlyGatewayLLMClient

_SEEN: List[Dict[str, Any]] = []


class _RecordingLLMClient(_ProtocolOnlyGatewayLLMClient):
    def prompt_cache_prepare_modules(self, **kwargs: Any) -> Dict[str, Any]:
        _SEEN.append({k: v for k, v in kwargs.items() if k in {"thinking", "namespace", "provider", "model"}})
        return super().prompt_cache_prepare_modules(**kwargs)


_BODY = {
    "provider": "stub",
    "model": "stub-model",
    "namespace": "tenant:stub-model",
    "modules": [{"module_id": "system", "system_prompt": "You are helpful"}],
}


def test_gateway_forwards_the_thinking_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _SEEN.clear()
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch, llm_client_cls=_RecordingLLMClient)
    with client:
        named = client.post("/api/gateway/prompt_cache/prepare_modules", json={**_BODY, "thinking": "xhigh"}, headers=headers)
        unnamed = client.post("/api/gateway/prompt_cache/prepare_modules", json=dict(_BODY), headers=headers)

    assert named.status_code == 200, named.text
    assert unnamed.status_code == 200, unnamed.text
    assert named.json()["supported"] is True and unnamed.json()["supported"] is True
    assert [seen.get("thinking", "<absent>") for seen in _SEEN] == ["xhigh", "<absent>"]
