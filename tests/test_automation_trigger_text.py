"""`trigger_text`: every automation's trigger in words (UX review #2), served on the summary next to
the schedule fields: the schedule sentence, "By hand", "When <feed> has a new item", "When <webhook>
is called", "When mail arrives in <address>"; never `feed.item@1`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, write_echo_bundle

from abstractgateway.automation_schedule import trigger_text


def test_the_words_per_source() -> None:
    t = lambda sid, **cfg: {"source_id": sid, "source_version": 1, "config": cfg}  # noqa: E731
    assert trigger_text(t("schedule", every="1h"), rule="Every hour (UTC)") == "Every hour (UTC)"
    assert trigger_text(t("manual"), rule="Manual runs only") == "By hand"
    assert trigger_text(t("feed.item", connection_id="c"), rule="feed.item@1", connection_label="Python news") == "When Python news has a new item"
    assert trigger_text(t("webhook.received", connection_id="c"), rule="x", connection_label="Orders") == "When Orders is called"
    assert trigger_text(t("email.received"), rule="When an email arrives", mailbox_address="me@x.test") == "When mail arrives in me@x.test"
    assert trigger_text(t("email.received"), rule="When an email arrives") == "When mail arrives"
    assert trigger_text(t("feed.item"), rule="feed.item@1") == "When the feed has a new item"
    assert trigger_text(t("acme.thing"), rule="acme.thing@1", source_label="When an ACME thing happens") == "When an ACME thing happens"
    assert trigger_text(t("acme.thing"), rule="acme.thing@1", source_label="Thing received") == "When thing received"


def test_summaries_carry_trigger_text_through_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        feed = c.post("/api/gateway/triggers/connections", headers=HEADERS, json={
            "source_id": "feed.item", "source_version": 1, "label": "Python news", "config": {"url": "http://127.0.0.1:9/feed.xml"}})
        hook = c.post("/api/gateway/triggers/connections", headers=HEADERS, json={
            "source_id": "webhook.received", "source_version": 1, "label": "Orders", "config": {}})
        assert feed.status_code == 201 and hook.status_code == 201, (feed.text, hook.text)

        def create(rid, trigger):
            r = c.post("/api/gateway/automations", headers=HEADERS, json={
                "request_id": rid, "title": rid, "target": {"bundle_ref": ref, "flow_id": ECHO_FLOW_ID}, "trigger": trigger})
            assert r.status_code == 200, r.text
            return r.json()["summary"]

        feed_row = create("f1", {"source_id": "feed.item", "source_version": 1, "config": {}, "connection_id": feed.json()["connection_id"]})
        hook_row = create("w1", {"source_id": "webhook.received", "source_version": 1, "config": {}, "connection_id": hook.json()["connection_id"]})
        hand_row = create("m1", {"source_id": "manual", "source_version": 1, "config": {}})
        sched_row = create("s1", {"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}})
        assert feed_row["trigger_text"] == "When Python news has a new item"
        assert hook_row["trigger_text"] == "When Orders is called"
        assert hand_row["trigger_text"] == "By hand"
        assert sched_row["trigger_text"] == sched_row["schedule_rule_text"] and "hour" in sched_row["trigger_text"].lower()
        assert feed_row["schedule_rule_text"]  # the old fields stay
        listed = {r["automation_id"]: r for r in c.get("/api/gateway/automations", headers=HEADERS).json()["items"]}
        assert listed[feed_row["automation_id"]]["trigger_text"] == "When Python news has a new item"
        assert all("@1" not in (r.get("trigger_text") or "") for r in listed.values())


def test_the_mailbox_address_is_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.mail import accounts
    from abstractgateway.routes import automations as routes

    class _Plane:
        primary = SimpleNamespace(name="primary")

        def for_account(self, account_id):
            return SimpleNamespace(name=account_id)

    addresses = {"primary": "me@home.test", "work": "me@work.test"}
    monkeypatch.setattr(accounts, "plane_for_service_config", lambda cfg: _Plane())
    monkeypatch.setattr(accounts, "mailbox_ids", lambda plane: ["default", "work"])
    monkeypatch.setattr(accounts, "account_store", lambda mb: SimpleNamespace(public=lambda: {"address": addresses[mb.name]}))
    svc = SimpleNamespace(config=SimpleNamespace(), host=SimpleNamespace(runtime=None))
    principal = SimpleNamespace(tenant_id="default", user_id="alice")
    primary = {"source_id": "email.received", "source_version": 1, "config": {}}
    work = {"source_id": "email.received", "source_version": 1, "config": {"account": "work"}}
    assert routes._trigger_text(svc, principal, primary, "When an email arrives") == "When mail arrives in me@home.test"
    assert routes._trigger_text(svc, principal, work, "When an email arrives") == "When mail arrives in me@work.test"


def test_the_tray_shows_trigger_text_in_rows_and_submenus() -> None:
    from abstractgateway.tray import menu_model as mm
    from abstractgateway.tray.sampler import automation_rows_from_listing

    rows = automation_rows_from_listing({"items": [
        {"automation_id": "f", "title": "News", "status": "active", "schedule_text": "feed.item@1",
         "trigger_text": "When Python news has a new item", "capabilities": ["pause", "run_now"]},
        {"automation_id": "s", "title": "Digest", "status": "paused", "schedule_text": "Every day at 08:00 (Europe/Paris) · next Mon 08:00",
         "trigger_text": "Every day at 08:00 (Europe/Paris)", "capabilities": ["resume"]},
        {"automation_id": "o", "title": "Old", "status": "active", "schedule_text": "Every 2 minutes (UTC)", "capabilities": []},
    ]})
    news, digest, old = rows
    assert mm.automation_row_label(news) == "⏱ News · When Python news has a new item"
    assert mm.automation_row_label(digest) == "⏸ Digest · paused · Every day at 08:00 (Europe/Paris) · next Mon 08:00"
    assert mm.automation_row_label(old) == "⏱ Old · Every 2 minutes (UTC)"  # an older gateway: schedule_text
    sub = mm.automation_submenu(news, observer=False, reachable=True, observer_path=None)
    assert sub[0].label == "Active · When Python news has a new item"
    assert [n.label for n in sub][-1] == "Pause"
    assert [n.label for n in mm.automation_submenu(digest, observer=False, reachable=True, observer_path=None)][-1] == "Resume"
    assert "feed.item@1" not in mm.automation_row_label(news)


def test_bundles_mark_triggerable_entrypoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path)
    defaults = {"schema_version": 1, "title": "T", "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
                "context": {"mode": "independent"}, "input_data": {}}
    ref = write_echo_bundle(tmp_path / "bundles", automation_defaults=defaults)
    from abstractgateway.app import app

    with TestClient(app) as c:
        items = c.get("/api/gateway/bundles", headers=HEADERS).json()["items"]
        row = next(i for i in items if f"{i['bundle_id']}@{i['bundle_version']}" == ref)
        flags = {ep["flow_id"]: ep["triggerable"] for ep in row["entrypoints"]}
        assert flags[ECHO_FLOW_ID] is True and flags["ask"] is False
