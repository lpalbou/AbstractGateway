"""ONE mind substrate per entity (maintainer ruling 2026-07-09 06:32).

The choice persists in the home (substrate.yaml), is exposed by GET/PUT
/{name}/substrate, and is resolved identically by visits and the own-time
loop: request override > home file > operator env > loud refusal. Never a
code default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from abstractgateway.entity_chat import (  # noqa: E402
    ChatOpenRefused,
    read_entity_substrate,
    resolve_substrate,
    write_entity_substrate,
)


def test_resolution_order_request_beats_home_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "castor"
    home.mkdir()
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)

    # Nothing anywhere: loud refusal (no code default).
    with pytest.raises(ChatOpenRefused):
        resolve_substrate(None, None, home_dir=home)

    # Operator env answers when the home carries no choice.
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "env-provider")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "env-model")
    assert resolve_substrate(None, None, home_dir=home) == ("env-provider", "env-model", None)

    # The home's persisted choice beats env (one substrate per entity).
    write_entity_substrate(home, provider="endpoint:ovh-provider", model="gpt-oss-120b")
    assert read_entity_substrate(home) == {"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b"}
    assert resolve_substrate(None, None, home_dir=home) == ("endpoint:ovh-provider", "gpt-oss-120b", None)

    # An explicit request override beats everything (still explicit).
    assert resolve_substrate("lmstudio", "tiny", home_dir=home) == ("lmstudio", "tiny", None)


def test_partial_choices_never_mix_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request naming only a provider fills the model from the SAME
    resolution chain — and refuses when no complete pair exists."""
    home = tmp_path / "e"
    home.mkdir()
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)
    with pytest.raises(ChatOpenRefused):
        resolve_substrate("lmstudio", None, home_dir=home)
    write_entity_substrate(home, provider="p1", model="m1")
    # Provider-only override + home model: explicit pieces, no silence.
    assert resolve_substrate("lmstudio", None, home_dir=home) == ("lmstudio", "m1", None)


def test_thinking_field_round_trips_and_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reasoning-first-citizen: the substrate's optional third field.

    Rules pinned here: the field is spelled `thinking` at rest; it writes
    only when set and reads back exactly; it resolves through the same
    chain as provider/model but NEVER refuses (a mind without a declared
    effort is valid); a request override of the effort wins over the file."""
    home = tmp_path / "castor"
    home.mkdir()
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)

    # Write with an effort: reads back as the triple.
    write_entity_substrate(home, provider="endpoint:ovh-provider", model="gpt-oss-120b", thinking="high")
    assert read_entity_substrate(home) == {
        "provider": "endpoint:ovh-provider",
        "model": "gpt-oss-120b",
        "thinking": "high",
    }
    assert resolve_substrate(None, None, home_dir=home) == ("endpoint:ovh-provider", "gpt-oss-120b", "high")

    # A request effort beats the stored one.
    assert resolve_substrate(None, None, home_dir=home, thinking="low") == (
        "endpoint:ovh-provider",
        "gpt-oss-120b",
        "low",
    )

    # Writing without an effort clears the field from the file (the writer
    # writes exactly what it is told; keep-semantics live at the PUT door).
    write_entity_substrate(home, provider="endpoint:ovh-provider", model="gpt-oss-120b")
    assert read_entity_substrate(home) == {"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b"}
    assert resolve_substrate(None, None, home_dir=home) == ("endpoint:ovh-provider", "gpt-oss-120b", None)


def test_write_requires_both_fields(tmp_path: Path) -> None:
    home = tmp_path / "e2"
    home.mkdir()
    with pytest.raises(ValueError):
        write_entity_substrate(home, provider="p", model="")
    # A malformed file reads as unset, never a crash.
    (home / "substrate.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    assert read_entity_substrate(home) == {}


def test_substrate_put_is_a_durable_marked_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """laurent 12:39 (hypnos incident): a mind swap must be answerable from
    the stream — 'which llm was behind during which time'. The PUT lands a
    principal-stamped old→new host marker BEFORE the file moves; the 12:24
    emergency flip (direct file edit, no event) is the gap this closes."""
    import copy
    import json

    from fastapi.testclient import TestClient

    pytest.importorskip("abstractmemory")
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "substrate-marker-secret")
    from abstractgateway.app import app

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = "Castor"
    spark["spark"] = 1
    with TestClient(app, headers={"Authorization": "Bearer substrate-marker-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": spark}).status_code == 201

        r = client.put(
            "/api/gateway/entities/Castor/substrate",
            json={"provider": "lmstudio", "model": "ornith-1.0-35b"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "entity"

        # The marker landed with old (unset) → new, stamped by the principal.
        from abstractgateway.service import get_gateway_service

        markers_path = (
            Path(get_gateway_service().config.data_dir) / "entities" / ".host_stream" / "castor.jsonl"
        )
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m for m in rows if m.get("payload", {}).get("kind") == "substrate_changed"]
        assert len(changed) == 1
        d = changed[0]["payload"]
        assert d["new"] == {"provider": "lmstudio", "model": "ornith-1.0-35b", "thinking": None}
        assert d["old"] == {"provider": None, "model": None, "thinking": None}
        assert d["by"] == "person:admin"

        # A second PUT records the transition old→new (the timeline).
        r2 = client.put(
            "/api/gateway/entities/Castor/substrate",
            json={"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b"},
        )
        assert r2.status_code == 200, r2.text
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m for m in rows if m.get("payload", {}).get("kind") == "substrate_changed"]
        assert len(changed) == 2
        d2 = changed[1]["payload"]
        assert d2["old"] == {"provider": "lmstudio", "model": "ornith-1.0-35b", "thinking": None}
        assert d2["new"] == {"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b", "thinking": None}

        # Reasoning effort through the door: setting it, keeping it through
        # a field-absent PUT (an older client can never erase it), and
        # clearing it with an explicit null.
        r3 = client.put(
            "/api/gateway/entities/Castor/substrate",
            json={"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b", "thinking": "high"},
        )
        assert r3.status_code == 200, r3.text
        assert r3.json()["thinking"] == "high"

        r4 = client.put(
            "/api/gateway/entities/Castor/substrate",
            json={"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b"},
        )
        assert r4.status_code == 200, r4.text
        assert r4.json()["thinking"] == "high", "a PUT without the field must KEEP the stored effort"

        r5 = client.put(
            "/api/gateway/entities/Castor/substrate",
            json={"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b", "thinking": None},
        )
        assert r5.status_code == 200, r5.text
        assert r5.json()["thinking"] is None, "an explicit null must CLEAR the effort"

        # The marker timeline recorded the effort transitions.
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m for m in rows if m.get("payload", {}).get("kind") == "substrate_changed"]
        assert len(changed) == 5
        assert changed[2]["payload"]["new"]["thinking"] == "high"
        assert changed[3]["payload"]["new"]["thinking"] == "high"
        assert changed[4]["payload"]["old"]["thinking"] == "high"
        assert changed[4]["payload"]["new"]["thinking"] is None

        # An effort-ONLY change (same provider/model) is a recorded mind
        # event too — "which effort was the mind at" needs this transition.
        r6 = client.put(
            "/api/gateway/entities/Castor/substrate",
            json={"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b", "thinking": "low"},
        )
        assert r6.status_code == 200, r6.text
        rows = [json.loads(line) for line in markers_path.read_text(encoding="utf-8").splitlines()]
        changed = [m for m in rows if m.get("payload", {}).get("kind") == "substrate_changed"]
        assert len(changed) == 6
        d6 = changed[5]["payload"]
        assert d6["old"]["model"] == d6["new"]["model"], "effort-only: model unchanged"
        assert d6["old"]["thinking"] is None and d6["new"]["thinking"] == "low"


def test_console_carries_the_reasoning_selectors() -> None:
    """The console's three reasoning selects exist with the full contract
    vocabulary — a vocab/option drift here turns into silent clears (the
    editor's set-select-value no-ops on a missing option)."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    for select_id in ("sandbox-reasoning", "entity-substrate-thinking", "entity-new-thinking"):
        assert f'id="{select_id}"' in html, select_id
    # Each select carries the six contract values.
    for value in ("none", "minimal", "low", "medium", "high", "xhigh"):
        assert html.count(f'<option value="{value}">') >= 3, f"{value} missing from a select"
