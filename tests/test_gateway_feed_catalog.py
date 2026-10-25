"""The feed catalogue New connection → Feed proposes (`feed_catalog.py`, `assets/feeds.json`).

The shipped JSON is well-formed (ids unique, https addresses, every field, templates hold their
placeholder once); a custom `<data_dir>/catalog/feeds.json` merges over it (same id replaces,
new id adds, `disabled: true` hides, a broken file or entry is skipped and named); the route
`GET /api/gateway/triggers/feeds/catalog` serves the merged list to any signed-in principal with
`source` per entry. The live check of every address is `scripts/check_feed_catalog.py` (network,
not run here)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway import feed_catalog as fc

ROUTE = "/api/gateway/triggers/feeds/catalog"


def _shipped() -> dict:
    return fc.shipped_catalog()


def test_shipped_catalog_is_well_formed() -> None:
    doc = _shipped()
    assert doc["version"] == 1
    feeds = doc["feeds"]
    assert 15 <= len(feeds) <= 25
    ids = [f["id"] for f in feeds]
    assert len(ids) == len(set(ids)), "ids must be unique"
    for f in feeds:
        assert fc.entry_problems(f, shipped=True) == [], f
        for key in fc.SHIPPED_REQUIRED:
            assert isinstance(f[key], str) and f[key].strip(), (f["id"], key)
        assert f["url"].startswith("https://") and f["site"].startswith("https://"), f["id"]
        assert "source" not in f and "disabled" not in f
    templates = [f for f in feeds if f.get("template")]
    assert {t["id"] for t in templates} == {"reddit-subreddit", "youtube-channel", "github-releases"}
    for t in templates:
        key = t["placeholder"]["key"]
        assert t["url"].count("{" + key + "}") == 1
        assert re.fullmatch(t["placeholder"]["pattern"], t["placeholder"]["example"])
        assert "{" + key + "}" in t["name_template"]
    # No X/Twitter (no open feed), no fixed entry with a placeholder.
    assert not any("twitter.com" in f["url"] or "x.com/" in f["url"] for f in feeds)
    assert {f["category"] for f in feeds} >= {"World news", "Technology", "Science", "Business and finance"}


def test_entry_problems_catches_what_the_console_relies_on() -> None:
    good = _shipped()["feeds"][0]
    assert fc.entry_problems({**good, "url": "http://example.org/feed"}, shipped=True)  # shipped = https only
    assert fc.entry_problems({**good, "url": "http://example.org/feed"}, shipped=False) == []
    assert fc.entry_problems({k: v for k, v in good.items() if k != "description"}, shipped=True)
    assert fc.entry_problems({**good, "id": "Bad Id"}, shipped=True)
    assert fc.entry_problems({**good, "url": "https://example.org/{x}"}, shipped=True)  # placeholder, not a template
    tpl = {**good, "url": "https://example.org/{a}/{a}", "template": True, "placeholder": {"key": "a", "label": "A", "example": "x"}}
    assert fc.entry_problems(tpl, shipped=True)  # twice
    tpl["url"] = "https://example.org/{a}"
    assert fc.entry_problems(tpl, shipped=True) == []
    tpl["placeholder"] = {**tpl["placeholder"], "pattern": "^[0-9]+$"}
    assert fc.entry_problems(tpl, shipped=True)  # example does not match


def test_merge_replaces_adds_and_hides() -> None:
    shipped = _shipped()
    first, second = shipped["feeds"][0]["id"], shipped["feeds"][1]["id"]
    custom = {"feeds": [
        {"id": "python-insider", "name": "Python Insider", "url": "https://blog.python.org/feeds/posts/default"},
        {"id": first, "name": "My BBC", "url": "https://feeds.bbci.co.uk/news/rss.xml"},
        {"id": second, "disabled": True},
        {"id": "broken", "name": "No address"},
        "not an object",
    ]}
    feeds, problems = fc.merge_catalogs(shipped, custom)
    ids = [f["id"] for f in feeds]
    assert ids[0] == first and second not in ids and ids[-1] == "python-insider"
    assert len(feeds) == len(shipped["feeds"]) - 1 + 1
    mine = feeds[0]
    assert mine["name"] == "My BBC" and mine["source"] == "custom" and mine["category"] == "My feeds"
    assert feeds[-1]["source"] == "custom" and all(f["source"] == "shipped" for f in feeds[1:-1])
    assert any("broken" in p for p in problems) and any("not an object" in p for p in problems)


def test_load_catalog_reads_the_custom_file_and_survives_a_broken_one(tmp_path: Path) -> None:
    out = fc.load_catalog(tmp_path)
    assert out["custom_path"] == str(tmp_path / "catalog" / "feeds.json")
    assert out["problems"] == [] and all(f["source"] == "shipped" for f in out["feeds"])
    path = tmp_path / "catalog" / "feeds.json"
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")
    out = fc.load_catalog(tmp_path)
    assert len(out["feeds"]) == len(_shipped()["feeds"]) and "not readable JSON" in out["problems"][0]
    path.write_text(json.dumps({"feeds": [{"id": "mine", "name": "Mine", "url": "https://example.org/feed.xml"}]}), encoding="utf-8")
    out = fc.load_catalog(tmp_path)
    assert out["feeds"][-1] == {"id": "mine", "name": "Mine", "url": "https://example.org/feed.xml", "category": "My feeds", "source": "custom"}


@pytest.fixture()
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    (tmp_path / "bundles").mkdir()
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    _a, alice = GatewayUserRegistry().create_user(user_id="alice", roles=["user"])
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c, tmp_path / "data", {"Authorization": f"Bearer {alice}"}


def test_route_serves_the_merged_catalog_to_any_signed_in_principal(gw) -> None:
    c, data_dir, alice = gw
    assert c.get(ROUTE).status_code == 401
    r = c.get(ROUTE, headers=alice)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and "custom_path" not in body and "problems" not in body
    shipped_ids = [f["id"] for f in _shipped()["feeds"]]
    assert [f["id"] for f in body["feeds"]] == shipped_ids and {f["source"] for f in body["feeds"]} == {"shipped"}
    custom = data_dir / "catalog" / "feeds.json"
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text(json.dumps({"feeds": [
        {"id": "python-insider", "name": "Python Insider", "url": "https://blog.python.org/feeds/posts/default"},
        {"id": shipped_ids[0], "disabled": True},
    ]}), encoding="utf-8")
    feeds = c.get(ROUTE, headers=alice).json()["feeds"]
    assert shipped_ids[0] not in [f["id"] for f in feeds]
    assert feeds[-1]["id"] == "python-insider" and feeds[-1]["source"] == "custom"
    admin = c.get(ROUTE, headers={"Authorization": "Bearer admin-token"}).json()
    assert admin["custom_path"] == str(custom) and admin["problems"] == []
