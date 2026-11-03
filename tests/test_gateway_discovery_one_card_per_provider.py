"""One provider, one card: a saved connection is never a second cloud provider card.

The operator's report (2026-11-03): after connecting Anthropic with an API key, the Providers tab
showed "Anthropic · Connected · 14 models" AND a second card "Anthropic (API key) · Not connected"
with an initials badge. The second card was the saved connection itself. The Gateway stores it in
AbstractCore's `provider_profiles` (the 2026-08-01 ruling), so AbstractCore's provider list ALSO
names it, as a bare `endpoint:anthropic-key` row; GET /discovery/providers kept that bare row and
dropped its own row for the same name, the one that carries `provider_endpoint_profile`, which is
how the kit's cards tell a connection from a provider.

The seam is read live on both sides: AbstractCore's real provider list (no stubbed rows) and the
VENDORED kit bundle's own card list (`console_islands.ISLANDS_JS`, `cloudProviders.client().listCards()`).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from node_requirement import require_node

from abstractgateway.console_islands import ISLANDS_JS
from test_gateway_provider_endpoint_profiles import _app_client

pytestmark = pytest.mark.basic

_HEADERS = {"Authorization": "Bearer admin-token"}


def _real_core_provider_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test app runs without a runner, so it has no discovery facade; use the same local query
    the live gateway's facade answers with: AbstractCore's registry, profiles included."""
    from abstractruntime.integrations.abstractcore.discovery_queries import local_list_providers

    class CoreListFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            return local_list_providers(include_models=False)

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            return {"provider": provider_name, "models": ["claude-test"]}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (CoreListFacade(), None))


def _kit_cards(discovery_payload: dict[str, Any]) -> list[dict[str, Any]]:
    node = require_node()
    probe = (
        "const vm = require('node:vm'); const fs = require('node:fs');"
        "const sb = { console }; sb.globalThis = sb; sb.window = sb; vm.createContext(sb);"
        "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sb);"
        "const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));"
        "const request = async (path) => { if (!path.endsWith('/discovery/providers')) throw new Error('unexpected ' + path); return payload; };"
        "sb.AfConsoleIslands.cloudProviders.client(request, '/api/gateway').listCards()"
        ".then((cards) => console.log(JSON.stringify(cards.map((c) => ({ id: c.id, name: c.name, family: c.family, kind: c.kind })))));"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(ISLANDS_JS)
        bundle = f.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as f:
        json.dump(discovery_payload, f)
        payload = f.name
    out = subprocess.run([node, "-e", probe, bundle, payload], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_saved_anthropic_key_is_the_anthropic_card_not_a_second_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_core_provider_list(monkeypatch)
    with _app_client(tmp_path, monkeypatch) as client:
        # Exactly what the kit's card saves: a fresh `<family>-key` id named "<Provider> (API key)".
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=_HEADERS,
            json={
                "id": "anthropic-key", "display_name": "Anthropic (API key)", "provider_family": "anthropic",
                "api_key": "sk-ant-test-only", "base_url": "", "scope": "gateway", "capabilities": ["text"],
                "create_only": True,
            },
        )
        assert created.status_code == 200, created.text
        listed = client.get("/api/gateway/discovery/providers", headers=_HEADERS)
        assert listed.status_code == 200, listed.text
        body = listed.json()

    rows = {str(r.get("name")): r for r in body["items"]}
    # AbstractCore really lists the connection (the precondition of the bug, not a stub).
    assert "endpoint:anthropic-key" in rows, sorted(rows)
    row = rows["endpoint:anthropic-key"]
    assert row["provider_endpoint_profile"]["id"] == "anthropic-key", row
    assert row["provider_family"] == "anthropic" and row["display_name"] == "Anthropic (API key)", row
    assert "sk-ant-test-only" not in listed.text
    # The registry row stays the provider: it carries no connection marker.
    assert "provider_endpoint_profile" not in rows["anthropic"], rows["anthropic"]

    cards = _kit_cards(body)
    names = [c["name"] for c in cards]
    assert [c for c in cards if c["family"] == "anthropic" or "Anthropic" in c["name"]] == [
        {"id": "anthropic", "name": "Anthropic", "family": "anthropic", "kind": "native"}
    ], names
    assert not [c for c in cards if c["id"].startswith("endpoint:")], names
    assert len(names) == len(set(names)), names
