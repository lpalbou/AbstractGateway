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

The expected artifacts are READ from AbstractCore's tier table (the hosts tested
are every tier's lower edge and just under its upper bound), never copied here:
a core release that moves the tiers (2.18.1: Qwen3 1.7B below 16 GiB, Qwen3.5 9B
16-32, Qwen3.8 27B 32-128, Flash-Next 128+) needs no edit to this file.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.basic

GIB = 1024**3

def _tiers():
    from abstractcore.config import model_catalog

    return model_catalog.APPLE_TEXT_TIERS


def _expected(gib: float, mtp: bool) -> str:
    """The artifact AbstractCore's table assigns to a Mac with `gib` GiB (each tier is
    `below_gib` exclusive; the last one is unbounded)."""
    for tier in _tiers():
        if tier["below_gib"] is None or gib < tier["below_gib"]:
            return tier["mtp"] if mtp and tier["mtp"] else tier["plain"]
    raise AssertionError("AbstractCore's APPLE_TEXT_TIERS must end with an unbounded tier")


def _tier_edges() -> list:
    """Every tier's lower edge (8 GiB for the first: the smallest Mac) and 0.1 GiB under its
    upper bound, plus one host well inside the unbounded tier."""
    hosts, lower = [], 8.0
    for tier in _tiers():
        hosts.append(lower)
        if tier["below_gib"] is None:
            hosts.append(lower * 1.5)
        else:
            hosts.append(round(tier["below_gib"] - 0.1, 1))
            lower = float(tier["below_gib"])
    return sorted(set(hosts))


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


@pytest.mark.parametrize("gib", _tier_edges())
def test_recommended_downloads_are_abstractcores_pick(on_host, mtp_switch, gib):
    from abstractcore.config.model_catalog import recommended_text_model
    from abstractgateway import core_config

    host = on_host(_host(gib))
    expected = _expected(gib, mtp_switch)
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
    assert ("mlx", _expected(32, mtp_switch)) in started


def test_the_guide_tile_carries_the_pick_and_its_fit_warning(on_host, mtp_switch, monkeypatch):
    """`/models/availability` -> `recommended.recommended[input.text]` is the
    tile: it names the tier artifact and, when the fit estimate doubts it,
    says so (the tier is never swapped silently). A Mac whose GPU limit is far
    below its memory (a 64 GiB Mac that lets a model use 10 GiB) keeps its
    memory tier, and the tile shows AbstractCore's own verdict and warning."""

    from abstractcore.config.model_catalog import recommended_text_model
    from abstractgateway import core_config

    host = dict(_host(64), ceiling_bytes=10 * GIB)
    on_host(host)
    monkeypatch.setattr(core_config, "gateway_capability_defaults_payload", lambda **kw: {"ok": True, "routes": []})
    payload = core_config.gateway_model_availability_payload()
    tile = _text(payload["recommended"]["recommended"])
    pick = recommended_text_model(host)
    assert tile["artifact"] == _expected(64, mtp_switch) == pick["artifact"]
    assert tile["catalog_id"] == pick["catalog_id"] and tile["basis"] == "apple_silicon_tiers"
    assert pick["fits"] is False and tile["fits"] is False
    assert tile["fit_verdict"] == pick["fit"]["verdict"] and tile["fit_verdict"] != "fits"
    assert tile["warning"] and tile["warning"] == pick["warning"]


@pytest.mark.parametrize("gib", _tier_edges())
def test_apply_recommended_writes_the_pick(on_host, mtp_switch, gib):
    from abstractgateway import core_config

    on_host(_host(gib))
    payload = core_config.apply_recommended_gateway_capability_defaults(dry_run=True)
    row = next(r for r in payload["applied_recommended"]["routes"] if r["key"] == "input.text")
    assert row["recommended"]["provider"] == "mlx"
    assert row["recommended"]["model"] == _expected(gib, mtp_switch)


def test_the_gateway_holds_no_tier_table(on_host, monkeypatch):
    """Swap AbstractCore's tier table: every Gateway answer must follow."""

    from abstractcore.config import model_catalog
    from abstractgateway import core_config

    original = _expected(64, False)
    swapped = tuple(
        dict(t, plain=t["mtp"], mtp=t["plain"]) for t in model_catalog.APPLE_TEXT_TIERS
    )
    monkeypatch.setattr(model_catalog, "APPLE_TEXT_TIERS", swapped)
    monkeypatch.setattr(model_catalog, "MTP_RECOMMENDED", False)
    on_host(_host(64))
    # A tier without an MTP build swaps to None: the pick falls back to the plain build.
    swapped_pick = next(t for t in swapped if t["below_gib"] is None or 64 < t["below_gib"])
    expected = swapped_pick["plain"] or swapped_pick["mtp"]
    assert expected != original
    assert _text(core_config.recommended_core_model_downloads())["artifact"] == expected
    report = core_config.apply_recommended_gateway_capability_defaults(dry_run=True)["applied_recommended"]
    assert next(r for r in report["routes"] if r["key"] == "input.text")["recommended"]["model"] == expected


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
    assert (text.capability, text.provider, text.model) == ("Text", "mlx", _expected(64, mtp_switch))


def test_the_guide_card_renders_the_fit_warning():
    """The Chat and text card shows AbstractCore's `warning` (and the tier rule)."""

    from abstractgateway import console

    source = console.__loader__.get_source(console.__name__)
    start = source.index("function renderFirstRunModel()")
    body = source[start : source.index("function firstRunCopy(", start)]
    assert "r.warning ?" in body and "esc(r.warning)" in body
    assert "esc(r.tier)" in body
