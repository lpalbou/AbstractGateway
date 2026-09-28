"""The guide's tiles on a Mac whose memory cannot hold the recommended image model.

AbstractCore (2026-09-28, `_FIT_GATED_ROUTES` gains `output.image`): FLUX.2
klein 4B (~8.5 GiB while it generates) is not written on an 8 GB Mac; the row
stays UNSET and carries `recommendation_unavailable` {provider, model, reason}.
The Gateway's surfaces -- the capability grid, `/models/availability` (the
guide's tiles), "Download all", `apply-recommended`, the tray's "Your
defaults" -- must survive that answer and carry the reason, never drop it
silently and never offer a download for it. A 16 GiB Mac is the contrast:
the image row is seeded and the image model is a normal tile.

Every expectation about WHICH routes a host cannot run is READ from
AbstractCore (`recommended_unavailable_routes(host)`), so these tests hold on
any Core: with 2.18.0 (no image gate) the 8 GiB Mac gets the image model, with
the gate it gets the reason. The Gateway holds no fit rule of its own.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.basic

GIB = 1024**3
IMAGE = "output.image"


def _host(gib: int, ceiling_source: str = "ram_75pct") -> dict:
    ram = gib * GIB
    return {
        "schema": "host_profile_v1",
        "os": "darwin",
        "arch": "arm64",
        "accelerator": "metal",
        "gpu_name": None,
        "gpu_count": 1,
        "unified_memory": True,
        "ram_bytes": ram,
        "vram_bytes": None,
        "ceiling_bytes": int(0.75 * ram),
        "ceiling_source": ceiling_source,
        "free_now_bytes": ram // 2,
        "disk": {},
        "notes": [],
    }


@pytest.fixture
def on_host(monkeypatch, tmp_path):
    """Pin AbstractCore's host probe to a synthetic Mac; a FRESH AbstractCore
    store (so the import-time seed runs against the synthetic host); empty
    model stores; no daemon reachable."""

    from abstractcore.utils import host_profile as hp

    hf = tmp_path / "hf" / "hub"
    hf.mkdir(parents=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(hf))
    monkeypatch.setenv("LMSTUDIO_MODELS_DIR", str(tmp_path / "lms"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("ABSTRACTCORE_LMS_CLI", str(tmp_path / "lms-not-installed"))
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "core" / "abstractcore.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "gw"))

    def pin(host):
        monkeypatch.setattr(hp, "host_profile", lambda **_k: dict(host))
        return host

    return pin


def _row(rows, key):
    return next(r for r in rows if isinstance(r, dict) and r.get("key") == key)


def _core_unavailable(host) -> dict:
    from abstractcore.config import capability_defaults as cd

    return cd.recommended_unavailable_routes(host)


HOSTS = [8, 16]


@pytest.mark.parametrize("gib", HOSTS)
def test_grid_row_carries_the_reason(on_host, gib):
    from abstractgateway import core_config

    host = on_host(_host(gib))
    expected = _core_unavailable(host).get(IMAGE)
    payload = core_config.gateway_capability_defaults_payload()
    row = _row(payload["routes"], IMAGE)
    if expected:
        assert row["configured"] is False
        assert row["recommendation_unavailable"] == expected
        assert row["recommendation_unavailable"]["reason"].strip()
    else:
        assert row["configured"] is True and row["provider"] == "mlx-gen"
        assert "recommendation_unavailable" not in row
    assert _row(payload["routes"], "input.text")["configured"] is True


@pytest.mark.parametrize("gib", HOSTS)
def test_availability_tiles(on_host, gib):
    """`/models/availability`: the plan (`recommended.recommended`, the
    Download cards) never lists a route this host cannot run; the grid rows
    (the web guide's `firstRunUnavailableCards`) carry its reason; text tile
    intact; no probe error."""

    from abstractgateway import core_config

    host = on_host(_host(gib))
    expected = _core_unavailable(host).get(IMAGE)
    payload = core_config.gateway_model_availability_payload()
    assert payload["ok"] is True and payload["errors"] == [], payload["errors"]
    plan = payload["recommended"]["recommended"]
    routes = {r["route"] for r in plan}
    assert "input.text" in routes
    text = next(r for r in plan if r["route"] == "input.text")
    assert text["provider"] == "mlx" and text["artifact"] and text["status"] in ("absent", "installed", "unknown")
    for item in plan:
        assert item.get("provider") and item.get("artifact"), item
    row = _row(payload["routes"], IMAGE)
    if expected:
        assert IMAGE not in routes
        assert IMAGE not in {g["route"] for g in payload["recommended"]["gaps"]}
        assert row["recommendation_unavailable"]["reason"] == expected["reason"]
    else:
        assert IMAGE in routes
        assert "recommendation_unavailable" not in row


@pytest.mark.parametrize("gib", HOSTS)
def test_recommended_downloads(on_host, gib):
    from abstractgateway import core_config

    host = on_host(_host(gib))
    expected = _core_unavailable(host)
    items = core_config.recommended_core_model_downloads()
    routes = {i["route"] for i in items}
    assert "input.text" in routes
    assert not (routes & set(expected)), (routes, expected)
    assert (IMAGE in routes) == (IMAGE not in expected)


@pytest.mark.parametrize("gib", HOSTS)
def test_apply_recommended_dry_run(on_host, gib):
    """The image entry says `unavailable` with Core's reason (the console
    message builder reads `action === "unavailable"` and `r.reason`)."""

    from abstractgateway import core_config

    host = on_host(_host(gib))
    expected = _core_unavailable(host).get(IMAGE)
    payload = core_config.apply_recommended_gateway_capability_defaults(dry_run=True)
    report = payload["applied_recommended"]
    entry = next(r for r in report["routes"] if r["key"] == IMAGE)
    text = next(r for r in report["routes"] if r["key"] == "input.text")
    assert text["recommended"]["provider"] == "mlx"
    if expected:
        assert entry["action"] == "unavailable", entry
        assert entry.get("reason") == expected["reason"], entry
        assert not entry.get("changed")
    else:
        assert entry["action"] != "unavailable", entry


@pytest.mark.parametrize("gib", HOSTS)
def test_download_all_skips_what_cannot_run(on_host, gib, monkeypatch):
    from abstractgateway import model_downloads

    host = on_host(_host(gib))
    expected = _core_unavailable(host)
    started = []
    monkeypatch.setattr(
        model_downloads,
        "start_download",
        lambda provider, artifact, dry_run=False: started.append((provider, artifact))
        or {"job_id": f"j{len(started)}", "provider": provider, "artifact": artifact},
    )
    out = model_downloads.start_recommended_group(dry_run=True)
    # (`out["group"]` is None here: its children are mock jobs Core's registry
    # does not know, and `group_view` forgets a parent over nothing.)
    assert all(c.get("job_id") for c in out["jobs"]), out
    assert not [c for c in out["jobs"] if c.get("status") == "failed"], out
    providers = {p for p, _a in started}
    assert "mlx" in providers
    if IMAGE in expected:
        assert "mlx-gen" not in providers, started
    else:
        assert "mlx-gen" in providers, started


@pytest.mark.parametrize("gib", HOSTS)
def test_tray_defaults_do_not_break(on_host, gib):
    from abstractgateway import core_config
    from abstractgateway.tray import menu_model

    on_host(_host(gib))
    payload = core_config.gateway_model_availability_payload()
    defaults = menu_model.parse_defaults(payload)
    tasks = {d.task for d in defaults}
    assert "text_generation" in tasks
    image_rows = [d for d in defaults if d.provider == "mlx-gen"]
    if IMAGE in _core_unavailable(_host(gib)):
        assert not image_rows
