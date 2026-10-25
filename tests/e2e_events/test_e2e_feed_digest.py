"""Example 2 (framework backlog 1048): "a digest of a news feed".

1. a `feed.item@1` connection on a feed URL (`POST /api/gateway/triggers/connections`,
   `initial_items: "deliver"` so the items already there make the first digest);
2. an automation bound to it running the "judge" flow;
3. the plane's worker polls the feed (here: one tick by hand; the gateway does it every ~30 s and
   fetches a connection at its own `every`, 15 minutes by default);
4. ONE occurrence carries the batch (`event.data.count`, `event.data.events[i].data.title`), the
   model writes the digest, and `trigger.event`, `run.finished` and `attention.raised` land on
   `GET /api/gateway/me/events`.

Hermetic: the feed is a RECORDED copy of the Python Insider blog's RSS (fetched 2026-10-25,
three items, descriptions shortened) served from a scratch loopback port. The `network` variant
reads the live feed and is skipped unless pytest runs with --allow-network."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from e2e_support import (
    HEADERS,
    ScriptedModel,
    attention_items,
    completed_occurrences,
    create_automation,
    events_of,
    gateway_env,
    wait_until,
    write_judge_bundle,
)

RECORDED = (Path(__file__).parent / "fixtures" / "python-insider.rss").read_bytes()
LIVE_FEED = "https://blog.python.org/feeds/posts/default"


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    write_judge_bundle(tmp_path / "bundles")
    model = ScriptedModel()
    model.install(monkeypatch)
    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service, wait_for_gateway_boot

    with TestClient(app) as c:
        assert wait_for_gateway_boot(60) == "ready"
        svc = get_gateway_service()
        # The plane's own worker would poll on its cadence; this example polls once, by hand.
        if getattr(svc, "email_worker", None) is not None:
            svc.email_worker.stop()
        yield c, model, svc


@pytest.fixture()
def recorded_feed():
    class Feed(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("ETag", '"recorded-2026-10-25"')
            self.end_headers()
            self.wfile.write(RECORDED)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Feed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/python-insider.rss"
    server.shutdown()


def _digest(client, model, svc, url: str, *, max_items: int = 3):
    from abstractgateway.trigger_connections import poll_feeds

    r = client.post("/api/gateway/triggers/connections", headers=HEADERS,
                    json={"source_id": "feed.item", "source_version": 1, "label": "Python Insider",
                          "config": {"url": url, "initial_items": "deliver", "max_items": max_items}})
    assert r.status_code == 201, r.text
    cid = r.json()["connection_id"]
    aid = create_automation(client, title="Python news digest", request_id="e2e-feed",
                            trigger={"source_id": "feed.item", "source_version": 1, "config": {}, "connection_id": cid},
                            prompt="New posts on a feed I follow. Write me a short digest if any of them matters.")
    model.answer({"title": "Python news", "body": "New Python releases are out; see the posts."})
    [report] = poll_feeds(svc)
    assert report["connection_id"] == cid and len(report["appended"]) >= 1 and aid in report["woken"], report
    [occ] = wait_until(lambda: completed_occurrences(client, aid), timeout_s=30)
    run = svc.host.run_store.load(occ["run_id"])
    return cid, aid, occ, run


def test_a_feed_batch_becomes_one_digest_with_attention_on_the_stream(live, recorded_feed) -> None:
    client, model, svc = live
    _cid, aid, occ, run = _digest(client, model, svc, recorded_feed)

    event = run.vars["event"]
    assert event["type"] == "abstract.feed.item.v1"
    titles = [e["data"]["title"] for e in event["data"]["events"]]
    assert event["data"]["count"] == 3
    assert sorted(titles) == sorted(["Python 3.15.0 (final) is here!", "Python 3.16.0 alpha 1 is now available",
                                     "Python 3.15.1, 3.14.9 and 3.13.17 are out"]), titles
    # The model read the items (framed as untrusted content in its prompt).
    assert any("Python 3.16.0 alpha 1" in p for p in model.prompts)

    [item] = attention_items(client, aid)
    assert item["title"] == "Python news"
    assert wait_until(lambda: events_of(client, "trigger.event", automation_id=aid))
    assert wait_until(lambda: events_of(client, "run.finished", run_id=occ["run_id"]))[0]["data"]["status"] == "completed"
    raised = wait_until(lambda: events_of(client, "attention.raised", automation_id=aid))
    assert len(raised) == 1 and raised[0]["data"]["kind"] == "notify"

    # A second poll of the same feed (nothing new) runs nothing and raises nothing.
    from abstractgateway.trigger_connections import poll_feeds

    assert all(not r.get("appended") for r in poll_feeds(svc))
    assert len(completed_occurrences(client, aid)) == 1


@pytest.mark.network("reads the live Python Insider feed (blog.python.org)")
def test_the_live_python_insider_feed(live) -> None:
    client, model, svc = live
    _cid, aid, occ, run = _digest(client, model, svc, LIVE_FEED, max_items=3)
    events = run.vars["event"]["data"]["events"]
    assert events and all(e["data"].get("title") for e in events)
    assert wait_until(lambda: events_of(client, "attention.raised", automation_id=aid))
