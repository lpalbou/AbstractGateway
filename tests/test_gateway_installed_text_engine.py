"""0958: the setup guide and the fresh-install defaults recommend a text engine that is installed.

Host facts as in the item: Linux + NVIDIA, LM Studio not installed (AbstractCore's portable pick, so its
plan row carries `engine_missing`), llama.cpp installed. AbstractCore's facade is stubbed: the rule is the
gateway's; the catalog rows carry this host's fit verdicts like `model_catalog_v1`."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from abstractgateway import core_config as cc

# AbstractCore's portable pick as the OVH server answered it (2026-10-29): no `engine_missing` (LM Studio is
# a server engine), status unknown; the engine inventory says LM Studio is not installed.
LMSTUDIO_PICK = {"route": "input.text", "provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@q4_k_m", "route_provider": "lmstudio",
                 "route_model": "qwen/qwen3.5-9b", "status": "unknown", "engine_missing": None,
                 "detail": "the `lms` CLI is not installed; GET http://localhost:1234/v1/models unreachable"}


def _art(provider: str, artifact: str, verdict: str = "fits", presence: str = "absent", size: int = 2_500_000_000) -> Dict[str, Any]:
    return {"provider": provider, "artifact": artifact, "fit": {"verdict": verdict}, "presence": {"status": presence}, "download_bytes": size}


CATALOG = {"rows": [
    {"id": "qwen3.5-9b", "display_name": "Qwen3.5 9B", "params_total": 9e9, "capabilities": {"text": True, "tools": "native"},
     "artifacts": [_art("lmstudio", "qwen/qwen3.5-9b@q4_k_m")]},
    {"id": "qwen3.5-4b", "display_name": "Qwen3.5 4B", "params_total": 4e9, "capabilities": {"text": True, "tools": "native"},
     "artifacts": [_art("huggingface", "unsloth/Qwen3.5-4B-GGUF:Q4_K_M"), _art("huggingface", "unsloth/Qwen3.5-4B-GGUF:Q8_0")]},
    {"id": "qwen3.8-27b", "display_name": "Qwen3.8 27B", "params_total": 27e9, "capabilities": {"text": True, "tools": "native"},
     "artifacts": [_art("huggingface", "unsloth/Qwen3.8-27B-GGUF:Q4_K_M", verdict="too_large", size=17_700_000_000)]},
    {"id": "llama-3.1-8b", "display_name": "Llama 3.1 8B", "params_total": 8e9, "capabilities": {"text": True, "tools": "native"},
     "artifacts": [_art("huggingface", "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M")]},
    {"id": "flux.2-klein-4b", "display_name": "FLUX.2 klein 4B", "params_total": 4e9, "capabilities": {"text": False, "image_generation": True},
     "artifacts": [_art("diffusers", "black-forest-labs/FLUX.2-klein-4B")]},
]}


@pytest.fixture()
def host(monkeypatch: pytest.MonkeyPatch):
    state = {"engines": {"lmstudio": False, "llamacpp": True}, "plan": [dict(LMSTUDIO_PICK)], "saved": [], "seeded": "recommended-v2",
             "routes": {"input.text": {"provider": "lmstudio", "model": "qwen/qwen3.5-9b"}}}
    f = cc.config_facade
    # This host's engines (the conftest default answers "unknown" so no test runs an engine CLI).
    monkeypatch.setattr(cc, "_engine_installed", lambda engine_id: state["engines"].get(engine_id))
    monkeypatch.setattr(f, "model_catalog", lambda *a, **k: CATALOG, raising=False)
    monkeypatch.setattr(f, "recommended_model_plan", lambda: {"recommended": [dict(r) for r in state["plan"]]}, raising=False)
    monkeypatch.setattr(cc, "_seed_marker", lambda **k: state["seeded"])
    monkeypatch.setattr(cc, "core_server_base_url", lambda: None)
    monkeypatch.setattr(cc, "capability_default_rows", lambda base_dir=None: dict(state["routes"]))
    monkeypatch.setattr(cc, "save_gateway_capability_default", lambda kind, modality=None, **kw: state["saved"].append((kind, modality, kw)) or {})
    return state


def test_a_missing_text_engine_is_replaced_by_a_fitting_gguf_through_llamacpp(host) -> None:
    pick = cc.installed_text_engine_pick(dict(LMSTUDIO_PICK))
    # Same family as AbstractCore's pick (Qwen), the largest that FITS (27B does not), Q4_K_M.
    assert pick["route_provider"] == "huggingface" and pick["route_model"] == "unsloth/Qwen3.5-4B-GGUF:Q4_K_M"
    assert pick["display_name"] == "Qwen3.5 4B" and "engine_missing" not in pick
    assert pick["alternative"]["route_provider"] == "lmstudio" and "not installed here" in pick["alternative"]["reason"]


@pytest.mark.parametrize("engines", [{"lmstudio": False, "llamacpp": False}, {"lmstudio": True, "llamacpp": True}])
def test_the_pick_stays_when_llamacpp_is_absent_or_lmstudio_is_installed(host, engines) -> None:
    host["engines"] = engines
    assert cc.installed_text_engine_pick(dict(LMSTUDIO_PICK)) is None


def test_the_setup_card_plan_shows_the_installed_engine_pick(host) -> None:
    out = cc._apply_installed_text_engine_rule({"recommended": [dict(LMSTUDIO_PICK), {"route": "output.voice", "provider": "supertonic", "artifact": "supertonic-3"}],
                                                "absent": [dict(LMSTUDIO_PICK)], "total": 2})
    assert out["recommended"][0]["route_provider"] == "huggingface" and out["recommended"][1]["provider"] == "supertonic"
    assert out["absent"][0]["artifact"] == "unsloth/Qwen3.5-4B-GGUF:Q4_K_M" and out["total"] == 2


def test_use_recommended_writes_the_installed_engine_pick(host) -> None:
    report = {"routes": [{"key": "input.text", "action": "apply", "changed": True, "after": {"provider": "lmstudio", "model": "qwen/qwen3.5-9b"}},
                         {"key": "output.voice", "action": "apply", "changed": True, "after": {"provider": "supertonic", "model": "supertonic-3"}}]}
    out = cc._apply_installed_text_engine_to_report(report, dry_run=False, base_dir=None)
    assert host["saved"] == [("input", "text", {"provider": "huggingface", "model": "unsloth/Qwen3.5-4B-GGUF:Q4_K_M", "base_url": "", "base_dir": None})]
    text = out["routes"][0]
    assert text["after"]["provider"] == "huggingface" and text["installed_engine"]["instead_of"]["provider"] == "lmstudio"
    assert text["download"] == {"provider": "huggingface", "artifact": "unsloth/Qwen3.5-4B-GGUF:Q4_K_M"}
    assert out["routes"][1]["after"]["provider"] == "supertonic"
    host["saved"].clear()
    cc._apply_installed_text_engine_to_report(report, dry_run=True, base_dir=None)
    assert host["saved"] == []  # a dry run writes nothing


def test_a_seeded_fresh_install_text_route_moves_but_an_operator_choice_never_does(host) -> None:
    moved = cc.correct_seeded_text_route()
    assert moved["to"] == {"provider": "huggingface", "model": "unsloth/Qwen3.5-4B-GGUF:Q4_K_M"} and len(host["saved"]) == 1
    host["saved"].clear()
    host["routes"]["input.text"] = {"provider": "openai", "model": "gpt-x"}  # the operator's own choice
    assert cc.correct_seeded_text_route() is None and host["saved"] == []
    host["routes"]["input.text"] = {"provider": "lmstudio", "model": "qwen/qwen3.5-9b"}
    host["seeded"] = None  # not a fresh-install seed
    assert cc.correct_seeded_text_route() is None and host["saved"] == []


def test_without_host_facts_the_rule_is_off(host, monkeypatch) -> None:
    # The engine inventory unreadable (None): AbstractCore's pick stays.
    monkeypatch.setattr(cc, "_engine_installed", lambda engine_id: None)
    assert cc.installed_text_engine_pick(dict(LMSTUDIO_PICK)) is None


def test_a_configured_route_on_an_lm_studio_that_is_not_installed_says_engine_missing(host) -> None:
    """OVH 8443 (2026-10-29): Text pointed at LM Studio, not installed; the grid said a green
    "configured" and every chat failed ("Circuit breaker open for LMStudioProvider")."""
    payload = {"routes": [
        {"key": "input.text", "provider": "lmstudio", "model": "qwen/qwen3.5-9b"},
        {"key": "output.image", "provider": "diffusers", "model": "black-forest-labs/FLUX.2-klein-4B"},
        {"key": "output.voice", "provider": "lmstudio", "model": "", "engine_missing": None},
    ]}
    out = cc.stamp_server_engine_missing(payload)
    text = out["routes"][0]["engine_missing"]
    assert text["engine"] == "lmstudio" and text["engine_row"] == "lmstudio" and "LM Studio is not installed" in text["reason"]
    assert "engine_missing" not in out["routes"][1]
    assert not out["routes"][2].get("engine_missing")  # no model: nothing configured to fail
    assert "engine_missing" not in payload["routes"][0]  # the input is not mutated
    host["engines"]["lmstudio"] = True
    assert "engine_missing" not in cc.stamp_server_engine_missing(payload)["routes"][0]
    host["engines"]["lmstudio"] = None  # inventory unreadable: never claimed missing
    assert "engine_missing" not in cc.stamp_server_engine_missing(payload)["routes"][0]


def test_the_capability_defaults_route_carries_the_stamp(host, monkeypatch, tmp_path) -> None:
    """The console's GET /config/capability-defaults (the payload the grid reads) carries the stamp."""
    from types import SimpleNamespace
    from abstractgateway.routes import gateway as gw

    monkeypatch.setattr(gw, "gateway_capability_defaults_payload", lambda base_dir=None: {"routes": [{"key": "input.text", "provider": "lmstudio", "model": "qwen/qwen3.5-9b"}]})
    monkeypatch.setattr(gw, "capability_defaults_route_problems", lambda base_dir: [])
    out = gw._capability_defaults_payload_for(SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path))))
    assert out["routes"][0]["engine_missing"]["engine"] == "lmstudio"
