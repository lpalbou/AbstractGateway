"""The guide's "Recommended for this computer" text tile is AbstractCore's pick.

Operator ruling 2026-09-24: on a Mac the recommended text model follows the
unified-memory tiers, and there is ONE owner of that choice --
`abstractcore.config.model_catalog.recommended_text_model()`. The Gateway's
guide tiles (`/models/availability` -> `recommended`), "Download all"
(`start_recommended_group`), `apply-recommended`, and the tray's "Your
defaults" (the routes the seed wrote) must all show that pick. These tests
drive the Gateway's OWN functions against a synthetic host, and prove the
Gateway holds no tier table of its own by swapping AbstractCore's table and
watching every Gateway answer follow.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.basic

GIB = 1024**3

EXPECTED = {
    # host GiB: (plain, mtp)
    16: ("mlx-community/Qwen3.5-9B-MLX-4bit", "mlx-works/Qwen3.5-9B-oQ4e-mtp"),
    23.9: ("mlx-community/Qwen3.5-9B-MLX-4bit", "mlx-works/Qwen3.5-9B-oQ4e-mtp"),
    24: ("mlx-community/Qwen3.8-27B-4bit", "Jundot/Qwen3.8-27B-oQ4e-mtp"),
    64: ("mlx-community/Qwen3.8-27B-4bit", "Jundot/Qwen3.8-27B-oQ4e-mtp"),
    127: ("mlx-community/Qwen3.8-27B-4bit", "Jundot/Qwen3.8-27B-oQ4e-mtp"),
    128: ("mlx-community/Qwen3.8-Flash-Next-4bit", "Jundot/Qwen3.8-Flash-Next-oQ4e-mtp"),
    192: ("mlx-community/Qwen3.8-Flash-Next-4bit", "Jundot/Qwen3.8-Flash-Next-oQ4e-mtp"),
}


def _host(gib: float, accelerator: str = "metal") -> dict:
    ram = int(gib * GIB)
    return {
        "schema": "host_profile_v1",
        "os": "darwin" if accelerator == "metal" else "linux",
        "arch": "arm64" if accelerator == "metal" else "x86_64",
        "accelerator": accelerator,
        "gpu_name": None,
        "gpu_count": 1,
        "unified_memory": accelerator == "metal",
        "ram_bytes": ram,
        "vram_bytes": None,
        "ceiling_bytes": int(0.75 * ram),
        "ceiling_source": "ram_75pct",
        "free_now_bytes": ram // 2,
        "disk": {},
        "notes": [],
    }


@pytest.fixture(params=[False, True], ids=["plain", "mtp"])
def mtp_switch(request, monkeypatch):
    from abstractcore.config import model_catalog

    monkeypatch.setattr(model_catalog, "MTP_RECOMMENDED", request.param)
    return request.param


@pytest.fixture
def on_host(monkeypatch, tmp_path):
    """Pin AbstractCore's host probe to a synthetic machine; empty model stores."""

    from abstractcore.utils import host_profile as hp

    hf = tmp_path / "hf" / "hub"
    hf.mkdir(parents=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(hf))
    monkeypatch.setenv("LMSTUDIO_MODELS_DIR", str(tmp_path / "lms"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("ABSTRACTCORE_LMS_CLI", str(tmp_path / "lms-not-installed"))

    def pin(host):
        monkeypatch.setattr(hp, "host_profile", lambda **_k: dict(host))
        return host

    return pin


def _text(items):
    return next(i for i in items if i["route"] == "input.text")


@pytest.mark.parametrize("gib", sorted(EXPECTED))
def test_recommended_downloads_are_abstractcores_pick(on_host, mtp_switch, gib):
    from abstractcore.config.model_catalog import recommended_text_model
    from abstractgateway import core_config

    host = on_host(_host(gib))
    expected = EXPECTED[gib][1 if mtp_switch else 0]
    item = _text(core_config.recommended_core_model_downloads())
    assert (item["provider"], item["artifact"]) == ("mlx", expected)
    pick = recommended_text_model(host)
    assert (item["provider"], item["artifact"]) == (pick["provider"], pick["artifact"])


@pytest.mark.parametrize("accelerator", ["cuda", "rocm", "none"])
def test_other_hosts_keep_the_portable_text_download(on_host, mtp_switch, accelerator):
    from abstractgateway import core_config

    on_host(_host(24, accelerator))
    item = _text(core_config.recommended_core_model_downloads())
    assert (item["provider"], item["artifact"]) == ("lmstudio", "qwen/qwen3.5-9b@4bit")


def test_download_all_fetches_the_pick(on_host, mtp_switch, monkeypatch):
    from abstractgateway import model_downloads

    on_host(_host(32))
    started = []
    monkeypatch.setattr(
        model_downloads,
        "start_download",
        lambda provider, artifact, dry_run=False: started.append((provider, artifact)) or {"job_id": f"j{len(started)}", "provider": provider, "artifact": artifact},
    )
    model_downloads.start_recommended_group(dry_run=True)
    assert ("mlx", EXPECTED[64][1 if mtp_switch else 0]) in started


def test_the_guide_tile_carries_the_pick_and_its_fit_warning(on_host, mtp_switch, monkeypatch):
    """`/models/availability` -> `recommended.recommended[input.text]` is the
    tile: it names the tier artifact and, when the fit estimate doubts it,
    says so (the tier is never swapped silently)."""

    from abstractgateway import core_config

    on_host(_host(24))
    monkeypatch.setattr(core_config, "gateway_capability_defaults_payload", lambda **kw: {"ok": True, "routes": []})
    payload = core_config.gateway_model_availability_payload()
    tile = _text(payload["recommended"]["recommended"])
    assert tile["artifact"] == EXPECTED[24][1 if mtp_switch else 0]
    assert tile["catalog_id"] == "qwen3.8-27b" and tile["basis"] == "apple_silicon_tiers"
    assert tile["fit_verdict"] == "too_large" and tile["fits"] is False
    assert "may not fit" in tile["warning"]


@pytest.mark.parametrize("gib", [23.9, 64, 128])
def test_apply_recommended_writes_the_pick(on_host, mtp_switch, gib):
    from abstractgateway import core_config

    on_host(_host(gib))
    payload = core_config.apply_recommended_gateway_capability_defaults(dry_run=True)
    row = next(r for r in payload["applied_recommended"]["routes"] if r["key"] == "input.text")
    assert row["recommended"]["provider"] == "mlx"
    assert row["recommended"]["model"] == EXPECTED[gib][1 if mtp_switch else 0]


def test_the_gateway_holds_no_tier_table(on_host, monkeypatch):
    """Swap AbstractCore's tier table: every Gateway answer must follow."""

    from abstractcore.config import model_catalog
    from abstractgateway import core_config

    swapped = tuple(
        dict(t, plain=t["mtp"], mtp=t["plain"]) for t in model_catalog.APPLE_TEXT_TIERS
    )
    monkeypatch.setattr(model_catalog, "APPLE_TEXT_TIERS", swapped)
    monkeypatch.setattr(model_catalog, "MTP_RECOMMENDED", False)
    on_host(_host(64))
    assert _text(core_config.recommended_core_model_downloads())["artifact"] == "Jundot/Qwen3.8-27B-oQ4e-mtp"
    report = core_config.apply_recommended_gateway_capability_defaults(dry_run=True)["applied_recommended"]
    assert next(r for r in report["routes"] if r["key"] == "input.text")["recommended"]["model"] == "Jundot/Qwen3.8-27B-oQ4e-mtp"


def test_tray_your_defaults_shows_the_seeded_pick_in_the_same_shape(on_host, mtp_switch, tmp_path, monkeypatch):
    """A fresh install on a 64 GiB Mac seeds the tier; the tray row that reads
    it keeps its shape (capability, task, provider, model, status)."""

    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway.tray import menu_model

    on_host(_host(64))
    store = tmp_path / "fresh" / "abstractcore.json"
    manager = ConfigurationManager(config_file=store, apply_env=False)
    rows = [dict(r, configured=bool(r.get("provider") and r.get("model"))) for r in manager.list_capability_defaults()]
    defaults = menu_model.parse_defaults({"routes": rows})
    text = next(d for d in defaults if d.task == "text_generation")
    assert type(text).__dataclass_fields__.keys() == {"capability", "task", "provider", "model", "status"}
    assert (text.capability, text.provider, text.model) == ("Text", "mlx", EXPECTED[64][1 if mtp_switch else 0])


def test_the_guide_card_renders_the_fit_warning():
    """The Chat and text card shows AbstractCore's `warning` (and the tier rule)."""

    from abstractgateway import console

    source = console.__loader__.get_source(console.__name__)
    start = source.index("function renderFirstRunModel()")
    body = source[start : source.index("function firstRunCopy(", start)]
    assert "r.warning ?" in body and "esc(r.warning)" in body
    assert "esc(r.tier)" in body
