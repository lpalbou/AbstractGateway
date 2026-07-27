"""Full tool catalog pins (tool-tiers item H build; operator dm#228).

The contract: /discovery/tools serves the FULL catalog — enabled rows as
before (now stamped enabled:true) PLUS every env-gated toolset's tools as
enabled:false rows with REAL specs (extracted from the real callables via
runtime's own normalizers, never fabricated) and the gate that disables
them. Exists-but-not-enabled is a visible state, never silence.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

_TOKEN = "catalog-test-token"


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    # A clean slate: no comms/agora/shell enablement leaking from the shell.
    for var in (
        "ABSTRACT_ENABLE_COMMS_TOOLS",
        "ABSTRACT_ENABLE_EMAIL_TOOLS",
        "ABSTRACT_ENABLE_WHATSAPP_TOOLS",
        "ABSTRACT_ENABLE_TELEGRAM_TOOLS",
        "ABSTRACT_ENABLE_AGORA_TOOLS",
        "ABSTRACT_ENABLE_SHELL_TOOLS",
        "AGORA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_disabled_toolsets_are_visible_rows_with_real_specs() -> None:
    """Laurent's complaint: email/telegram tools exist but discovery could
    not see them. Now: enabled:false rows with the real spec + the gate."""
    with _client() as client:
        r = client.get("/api/gateway/discovery/tools")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        by_name = {t["name"]: t for t in items}

        for name in ("send_email", "read_email", "list_emails", "list_email_accounts",
                     "send_telegram_message", "send_whatsapp_message"):
            assert name in by_name, f"{name} missing from the catalog"
            row = by_name[name]
            assert row["enabled"] is False
            # runtime shipped sub-toolset granularity same-hour (comms.email
            # / comms.whatsapp / comms.telegram) answering the filed gap.
            assert str(row["toolset"]).startswith("comms")
            assert "ABSTRACT_ENABLE" in row["enable_gate"]
            # Real spec, never fabricated: the description comes from the callable.
            assert isinstance(row.get("description"), str) and row["description"].strip()

        # Persistent shell: deliberate opt-in, visible-disabled.
        assert by_name["shell_exec"]["enabled"] is False
        assert "0220" in by_name["shell_exec"]["why_disabled"] or "opt-in" in by_name["shell_exec"]["why_disabled"]

        # Agora: the both-halves gate is named (intent AND key — c4211).
        assert by_name["agora_post_message"]["enabled"] is False
        assert "AGORA_API_KEY" in by_name["agora_post_message"]["enable_gate"]


def test_enabled_rows_carry_enabled_true_and_no_duplicates() -> None:
    """STRICT global name-uniqueness (adversary F6: a cross-lane duplicate
    is exactly the drift/race shape — a name in both the enabled and the
    disabled lane must fail here, whatever toolset label it wears)."""
    with _client() as client:
        items = _client_items(client)
        names = [t["name"] for t in items]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"duplicate catalog rows: {sorted(dupes)}"
        by_name = {t["name"]: t for t in items}
        # The always-on lanes are enabled:true.
        assert by_name["read_file"]["enabled"] is True
        assert by_name["execute_command"]["enabled"] is True


def _client_items(client: TestClient) -> list:
    r = client.get("/api/gateway/discovery/tools")
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_enabling_a_gate_moves_rows_to_the_enabled_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """A toolset is in the enabled lane OR the disabled list — never both,
    never neither (the one-predicate-source rule)."""
    monkeypatch.setenv("ABSTRACT_ENABLE_EMAIL_TOOLS", "1")
    with _client() as client:
        items = _client_items(client)
        email_rows = [t for t in items if t["name"] == "send_email"]
        assert len(email_rows) == 1, "send_email must appear exactly once"
        assert email_rows[0]["enabled"] is True
        # Un-enabled comms kinds remain disabled rows.
        wa = [t for t in items if t["name"] == "send_whatsapp_message"]
        assert len(wa) == 1 and wa[0]["enabled"] is False


def test_disabled_rows_never_serve_auto_approval() -> None:
    """Adversary F3: runtime's pre-tiers approval fold carries AUTO rows for
    telegram and agora_* — serving auto on a DISABLED row would pre-approve
    a tool the operator never enabled (and undercut the pending send_email
    re-ruling). The clamp forces ask on every disabled row, and the pin
    targets the names that WOULD have been auto, not just send_email
    (which is require-approval anyway and could never fail)."""
    with _client() as client:
        items = _client_items(client)
        for name in ("send_telegram_message", "agora_post_message", "send_email", "shell_exec"):
            row = next((t for t in items if t["name"] == name), None)
            assert row is not None, f"{name} missing from catalog"
            assert row["enabled"] is False
            if row.get("approval_default") is not None:
                assert row["approval_default"] == "ask", f"{name} served {row['approval_default']} while disabled"


def test_whatsapp_enablement_moves_only_whatsapp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second enablement case (adversary F6): a sibling comms gate flips one
    kind to the enabled lane and leaves the others disabled — no duplicates,
    no vanishing."""
    monkeypatch.setenv("ABSTRACT_ENABLE_WHATSAPP_TOOLS", "1")
    with _client() as client:
        items = _client_items(client)
        names = [t["name"] for t in items]
        assert names.count("send_whatsapp_message") == 1
        assert next(t for t in items if t["name"] == "send_whatsapp_message")["enabled"] is True
        assert next(t for t in items if t["name"] == "send_email")["enabled"] is False


def test_see_also_points_at_the_other_discovery_surfaces() -> None:
    """Life-tools live on the entity inventory (grantable:false by plane —
    the adopted guard); MCP servers on their declared registry. The catalog
    response carries the map so no consumer re-derives it."""
    with _client() as client:
        r = client.get("/api/gateway/discovery/tools")
        see = r.json()["see_also"]
        assert "entities/inventory/tools" in see["entity_inventory"]
        assert "mcp/servers" in see["mcp_servers"]


def test_factless_disabled_rows_stamp_unvetted_top() -> None:
    """core c4577: factless env-gated rows derive unvetted at the TOP of the
    ladder (rank 4, presentation=unvetted — never rendered 'destructive',
    never silently low). Feature-detected: older core = unstamped rows."""
    pytest.importorskip("abstractcore.tools.risk_facts")
    with _client() as client:
        items = _client_items(client)
        # agora tools have no declared facts yet — genuinely factless
        # (send_email graduated to declared outreach facts in core v3).
        row = next(t for t in items if t["name"] == "agora_post_message")
        assert row["enabled"] is False
        # The ruled wire shape (c4589/c4592/c4599): word on risk_tier,
        # integer on risk_rank, presentation always present.
        assert row["risk_rank"] == 4
        assert isinstance(row["risk_tier"], str)
        assert row["risk_presentation"] == "unvetted"
        assert row.get("risk_mapping_version") is not None


def test_plugin_load_errors_surface_on_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """camera c4634 boot-race class: a capability plugin whose register()
    failed at entry-point load is invisible to the catalog lane (the error
    lives in core's registry status, process-internal) — the discovery
    response must surface it, never serve silent absence."""
    from abstractgateway import tool_catalog as tc

    monkeypatch.setattr(
        tc, "plugin_error_warnings",
        lambda: ["#FALLBACK capability plugin 'camera' failed to load: boot raced a tree rewrite — its tools are absent from this catalog (bounce after the tree is quiescent)"],
    )
    # Route must carry the warning through (the route imports the symbol
    # from the module at call time, so the monkeypatch reaches it).
    with _client() as client:
        r = client.get("/api/gateway/discovery/tools")
        assert r.status_code == 200
        warns = r.json().get("catalog_warnings") or []
        assert any("camera" in w and "failed to load" in w for w in warns)


def test_plugin_error_warnings_never_raise() -> None:
    from abstractgateway.tool_catalog import plugin_error_warnings

    out = plugin_error_warnings()
    assert isinstance(out, list)


def test_discovery_rows_derive_honest_bands_not_all_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observer c4647 live gap: every discovery row served destroy/4/unvetted
    because the prompt-lane specs carry no facts — the fold's input was
    missing, not the fold. The registry-facts join must produce a real
    ladder: read_file observes, write_file acts, execute_command destroys —
    and rows with no declared facts stay unvetted-at-top (honest)."""
    pytest.importorskip("abstractcore.tools.risk_facts")
    with _client() as client:
        items = _client_items(client)
        by_name = {t["name"]: t for t in items}
        assert by_name["read_file"]["risk_rank"] == 1, by_name["read_file"]
        assert by_name["read_file"]["risk_tier"] == "observe"
        assert by_name["write_file"]["risk_rank"] == 2
        assert by_name["execute_command"]["risk_rank"] == 4
        assert by_name["execute_command"]["risk_presentation"] != "unvetted"
        # send_email (disabled row, facts declared in core v3): honest
        # outreach, no longer unvetted-at-top.
        assert by_name["send_email"]["risk_rank"] == 3
        assert by_name["send_email"]["enabled"] is False
        # DECLARED rows ladder; undeclared rows (agora/shell, and camera
        # until its fact declarations reach the installed plugin) sit at
        # unvetted rank 4 — deny-safe honesty, not a wall of fake destroys:
        # every fact-DECLARED builtin must NOT read unvetted.
        declared = [t for t in items if t["name"] in ("read_file", "list_files", "web_search", "write_file", "edit_file", "fetch_url", "execute_command", "send_email")]
        assert all(t["risk_presentation"] != "unvetted" for t in declared)
        ranks = {t["name"]: t["risk_rank"] for t in declared}
        assert ranks["read_file"] < ranks["write_file"] <= ranks["send_email"] <= ranks["execute_command"]


# ---------------------------------------------------------------- boundary


def test_comms_kind_map_matches_runtime_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    """DRIFT PIN for _COMMS_KIND_TOOLS (import-boundary fix 2026-07-23): the
    gateway's kind->names map exists only because list_tool_catalog is
    toolset-granular and the boundary contract forbids importing the
    callables from core. Runtime's COMPOSITION is the truth — enable all
    comms kinds and assert the map equals the composed toolset's names, so
    a runtime-side membership change breaks THIS test, never silently thins
    the remainder lane."""
    monkeypatch.setenv("ABSTRACT_ENABLE_COMMS_TOOLS", "1")
    from abstractruntime.integrations.abstractcore.default_tools import get_default_toolsets

    from abstractgateway.tool_catalog import _COMMS_KIND_TOOLS

    comms = get_default_toolsets().get("comms") or {}
    composed = {getattr(fn, "__name__", "") for fn in comms.get("tools") or []}
    mapped = {name for names in _COMMS_KIND_TOOLS.values() for name in names}
    assert composed, "comms toolset did not compose with the gate enabled"
    assert mapped == composed, (
        f"kind map drifted from runtime's composition: map-only={sorted(mapped - composed)}, "
        f"runtime-only={sorted(composed - mapped)}"
    )


def test_registry_rows_carry_facts_and_unknown_names_degrade_labeled() -> None:
    """The disabled comms lanes build rows from the RUNTIME FACADE's registry
    rows (no callable import, boundary contract): a known name serves the
    full core-authored row (facts + description + parameters) stamped
    disabled; an unknown name serves a labeled name-only row — visible,
    never silent."""
    from abstractgateway.tool_catalog import _registry_rows_by_name, _rows_from_registry

    warnings: list[str] = []
    registry = _registry_rows_by_name(warnings)
    assert "send_email" in registry, "core registry rows unavailable through the runtime facade"
    rows = _rows_from_registry(
        ("send_email", "not_a_real_tool_xyz"), registry,
        toolset="comms", gate="GATE", why="why", warnings=warnings,
    )
    by_name = {r["name"]: r for r in rows}
    full = by_name["send_email"]
    assert full["enabled"] is False and full["enable_gate"] == "GATE"
    assert full.get("comms_send") is True  # the core-authored FACT rode through
    assert isinstance(full.get("parameters"), dict) and full.get("description")
    stub = by_name["not_a_real_tool_xyz"]
    assert stub["enabled"] is False and "description" not in stub
    assert any("name-only" in w for w in warnings)


def test_plugin_error_warnings_probe_runtime_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """plugin_error_warnings reaches core's registry status ONLY through a
    runtime export (`default_tools.capability_plugin_errors`, asked of
    runtime 2026-07-23): when the export exists the errors surface; on
    today's runtime (no export) the lane degrades to [] — labeled in code,
    pinned here so the probe lights up the day runtime ships it."""
    from abstractruntime.integrations.abstractcore import default_tools as dt

    from abstractgateway.tool_catalog import plugin_error_warnings

    monkeypatch.setattr(
        dt, "capability_plugin_errors",
        lambda: [{"name": "camera", "error": "boot raced a tree rewrite"}],
        raising=False,
    )
    warns = plugin_error_warnings()
    assert any("camera" in w and "boot raced" in w for w in warns)

    monkeypatch.delattr(dt, "capability_plugin_errors", raising=False)
    assert plugin_error_warnings() == []
