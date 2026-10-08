"""R16.1 (gateway): schedule@2 through the API, the account `time_zone` preference, and the
served next run + schedule sentence on every automation summary."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, legacy_schedule_run, save_runs, write_echo_bundle

HOST_TZ = "Asia/Tokyo"


@pytest.fixture()
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=False)
    monkeypatch.setenv("TZ", HOST_TZ)  # the host's zone, as the OS reports it
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


@pytest.fixture()
def gw_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    monkeypatch.setenv("TZ", HOST_TZ)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _create(c, trigger, request_id="r1"):
    body = {
        "request_id": request_id,
        "title": f"Cal {request_id}",
        "target": {"bundle_ref": c.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "hi"}},
        "trigger": trigger,
        "context": {"mode": "independent"},
    }
    r = c.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _get(c, aid):
    r = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _prefs(c, body=None):
    if body is None:
        r = c.get("/api/gateway/accounts/me/preferences", headers=HEADERS)
    else:
        r = c.put("/api/gateway/accounts/me/preferences", headers=HEADERS, json=body)
    return r


DAILY = {"source_id": "schedule", "source_version": 2, "config": {"kind": "daily", "at": "08:00"}}


# ---------------------------------------------------------------- preferences (A2)


def test_time_zone_preference_defaults_to_the_host_zone_and_is_editable(gw):
    r = _prefs(gw)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preferences"]["time_zone"] is None
    block = body["time_zone"]
    assert block["value"] is None and block["gateway_default"] == HOST_TZ and block["effective"] == HOST_TZ
    assert "Europe/Paris" in block["choices"] and block["choices"] == sorted(block["choices"])
    assert body["declared"]["time_zone"]["label"] == "Time zone"

    r = _prefs(gw, {"time_zone": "Europe/Paris"})
    assert r.status_code == 200, r.text
    assert r.json()["time_zone"]["effective"] == "Europe/Paris" and r.json()["preferences"]["time_zone"] == "Europe/Paris"
    # The default_workflow entries are untouched by a time_zone write.
    assert set(r.json()["preferences"]["default_workflow"]) == set(body["preferences"]["default_workflow"])

    r = _prefs(gw, {"time_zone": "Mars/Olympus"})
    assert r.status_code == 400 and r.json()["detail"]["key"] == "time_zone"
    assert "IANA" in r.json()["detail"]["message"]
    assert _prefs(gw).json()["time_zone"]["value"] == "Europe/Paris"  # a refusal changes nothing

    r = _prefs(gw, {"time_zone": None})
    assert r.status_code == 200 and r.json()["time_zone"] == {**r.json()["time_zone"], "value": None, "effective": HOST_TZ}

    r = _prefs(gw, {"timezone": "Europe/Paris"})
    assert r.status_code == 400 and "Unknown preference 'timezone'" in r.json()["detail"]["message"]


def test_host_time_zone_reads_the_os(tmp_path):
    from abstractgateway.automation_schedule import host_time_zone

    link = tmp_path / "localtime"
    (tmp_path / "zoneinfo" / "America").mkdir(parents=True)
    target = tmp_path / "zoneinfo" / "America" / "Los_Angeles"
    target.write_text("x")
    link.symlink_to(target)
    assert host_time_zone(environ={}, localtime=str(link), timezone_file=str(tmp_path / "none")) == "America/Los_Angeles"
    assert host_time_zone(environ={"TZ": ":Europe/Paris"}, localtime=str(link)) == "Europe/Paris"
    assert host_time_zone(environ={"TZ": "garbage"}, localtime=str(link)) == "America/Los_Angeles"
    (tmp_path / "timezone").write_text("Africa/Nairobi\n")
    assert host_time_zone(environ={}, localtime=str(tmp_path / "missing"), timezone_file=str(tmp_path / "timezone")) == "Africa/Nairobi"
    assert host_time_zone(environ={}, localtime=str(tmp_path / "missing"), timezone_file=str(tmp_path / "missing")) == "UTC"


# ------------------------------------------------------ creation / revision (A1, A2)


def test_a_rule_without_a_zone_takes_the_owners_preference_then_the_host_zone(gw):
    out = _create(gw, DAILY, "host")
    assert _get(gw, out["automation_id"])["definition"]["trigger"]["config"]["time_zone"] == HOST_TZ
    _prefs(gw, {"time_zone": "Europe/Paris"})
    out = _create(gw, DAILY, "pref")
    cfg = _get(gw, out["automation_id"])["definition"]["trigger"]["config"]
    assert cfg["time_zone"] == "Europe/Paris" and cfg["kind"] == "daily" and cfg["at"] == "08:00"
    explicit = {"source_id": "schedule", "source_version": 2, "config": {"kind": "daily", "at": "08:00", "time_zone": "America/New_York"}}
    out = _create(gw, explicit, "explicit")
    assert _get(gw, out["automation_id"])["definition"]["trigger"]["config"]["time_zone"] == "America/New_York"


def test_revise_keeps_the_binding_zone_unless_the_client_changes_it(gw_live):
    gw = gw_live
    _prefs(gw, {"time_zone": "Europe/Paris"})
    aid = _create(gw, DAILY)["automation_id"]
    _prefs(gw, {"time_zone": "Asia/Kolkata"})  # a later preference change never moves an existing rule
    weekly = {"source_id": "schedule", "source_version": 2, "config": {"kind": "weekly", "days": ["mon"], "at": "09:00"}}
    r = gw.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={"command_id": "c1", "changes": {"trigger": weekly}})
    assert r.status_code == 200, r.text
    from automations_fixtures import wait_until

    def revised():
        d = _get(gw, aid)
        return d if d["definition"]["revision"] == 2 else None

    d = wait_until(revised, timeout_s=15)
    assert d["definition"]["trigger"]["config"]["time_zone"] == "Europe/Paris"
    assert d["definition"]["trigger"]["config"]["days"] == ["mon"]
    assert d["summary"]["schedule_rule_text"] == "Every Mon at 09:00 (Europe/Paris)"
    moved = {"source_id": "schedule", "source_version": 2, "config": {**d["definition"]["trigger"]["config"], "time_zone": "America/Chicago"}}
    r = gw.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={"command_id": "c2", "changes": {"trigger": moved}})
    assert r.status_code == 200, r.text
    d = wait_until(lambda: (lambda x: x if x["definition"]["revision"] == 3 else None)(_get(gw, aid)), timeout_s=15)
    assert d["definition"]["trigger"]["config"]["time_zone"] == "America/Chicago"
    assert d["summary"]["time_zone"] == "America/Chicago"


def test_invalid_zone_is_refused_with_the_field(gw):
    bad = {"source_id": "schedule", "source_version": 2, "config": {"kind": "daily", "at": "08:00", "time_zone": "Nowhere/City"}}
    r = gw.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "bad", "title": "x", "target": {"bundle_ref": gw.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {}},
        "trigger": bad})
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "trigger.config.time_zone"


def test_schedule_v1_create_stays_v1_byte_identical(gw):
    v1 = {"source_id": "schedule", "source_version": 1, "config": {"start_at": "2026-10-01T00:00:00Z", "every": "2m"}}
    aid = _create(gw, v1)["automation_id"]
    trig = _get(gw, aid)["definition"]["trigger"]
    assert trig["source_version"] == 1
    assert trig["config"] == {"start_at": "2026-10-01T00:00:00+00:00", "anchor": "2026-10-01T00:00:00+00:00", "every": "2m"}


def test_trigger_sources_list_schedule_v2(gw):
    items = {(i["id"], i["version"]): i for i in gw.get("/api/gateway/trigger-sources", headers=HEADERS).json()["items"]}
    assert items[("schedule", 1)]["available"] and items[("schedule", 2)]["available"]
    assert items[("schedule", 2)]["config_schema"]["properties"]["kind"]["enum"] == ["every", "once", "daily", "weekly", "monthly"]


# ------------------------------------------------------------- summary (A4)


def test_summary_serves_next_run_and_the_sentence(gw):
    _prefs(gw, {"time_zone": "Europe/Paris"})
    trig = {"source_id": "schedule", "source_version": 2,
            "config": {"kind": "daily", "at": "08:00", "start_at": "2030-01-01T00:00:00Z"}}
    out = _create(gw, trig)
    s = out["summary"]
    assert s["next_fire_at"] == "2030-01-01T07:00:00+00:00"
    assert s["next_run_at"] == s["next_fire_at"]
    assert s["next_run_local"] == "2030-01-01T08:00:00+01:00"
    assert s["time_zone"] == "Europe/Paris"
    assert s["schedule_rule_text"] == "Every day at 08:00 (Europe/Paris)"
    assert s["schedule_text"] == "Every day at 08:00 (Europe/Paris) · next Tue 1 Jan 2030 08:00"
    listed = gw.get("/api/gateway/automations", headers=HEADERS).json()["items"]
    row = next(i for i in listed if i["automation_id"] == out["automation_id"])
    assert {k: row[k] for k in ("next_run_at", "next_run_local", "time_zone", "schedule_text")} == {
        k: s[k] for k in ("next_run_at", "next_run_local", "time_zone", "schedule_text")}


def test_summary_next_run_is_the_runtime_projection_not_a_gateway_computation(gw, monkeypatch):
    """Served vs computed: whatever the runtime projects is what every client gets."""
    _prefs(gw, {"time_zone": "Europe/Paris"})
    aid = _create(gw, DAILY)["automation_id"]
    import abstractruntime.automation_queries as aq

    monkeypatch.setattr(aq, "_next_fire_at", lambda run, **kw: "2031-07-14T10:15:00+00:00")
    s = _get(gw, aid)["summary"]
    assert s["next_run_at"] == "2031-07-14T10:15:00+00:00"
    assert s["next_run_local"] == "2031-07-14T12:15:00+02:00"
    assert s["schedule_text"].endswith("· next Mon 14 Jul 2031 12:15")


def test_v1_and_manual_rows_use_the_owner_zone(gw):
    _prefs(gw, {"time_zone": "Europe/Paris"})
    v1 = {"source_id": "schedule", "source_version": 1, "config": {"start_at": "2030-06-01T00:00:00Z", "every": "24h"}}
    s = _create(gw, v1, "v1")["summary"]
    assert s["time_zone"] == "Europe/Paris"
    assert s["schedule_text"] == "Every 24 hours (UTC) · next Sat 1 Jun 2030 02:00 (Europe/Paris)"
    s = _create(gw, {"source_id": "manual", "source_version": 1, "config": {}}, "m")["summary"]
    assert s["schedule_text"] == "Manual runs only" and "next_run_at" not in s and s["time_zone"] == "Europe/Paris"


def test_legacy_rows_carry_the_sentence(gw):
    save_runs(legacy_schedule_run())
    rows = gw.get("/api/gateway/automations?archived_only=false", headers=HEADERS).json()["items"]
    legacy = [r for r in rows if r.get("legacy")]
    assert legacy and all(r["schedule_rule_text"] and r["time_zone"] == HOST_TZ for r in legacy)


# ------------------------------------------------------------------ preview


def test_schedule_preview_is_the_gateways_sentence(gw, monkeypatch):
    _prefs(gw, {"time_zone": "Europe/Paris"})
    import abstractgateway.routes.automations as routes

    monkeypatch.setattr(routes, "_now_iso", lambda: "2026-10-08T12:00:00+00:00")
    r = gw.post("/api/gateway/automations/schedule-preview", headers=HEADERS, json={"trigger": DAILY})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["trigger"]["config"]["time_zone"] == "Europe/Paris"
    assert p["next_run_at"] == "2026-10-09T06:00:00+00:00"
    assert p["next_run_local"] == "2026-10-09T08:00:00+02:00"
    assert p["schedule_text"] == "Every day at 08:00 (Europe/Paris) · next Fri 9 Oct 08:00"
    assert p["first_run_sentence"] == "Runs every day at 08:00 (Europe/Paris), first run Fri 9 Oct 08:00."
    every = {"source_id": "schedule", "source_version": 2, "config": {"kind": "every", "every": "24h"}}
    assert gw.post("/api/gateway/automations/schedule-preview", headers=HEADERS, json={"trigger": every}).json()["first_run_sentence"] == \
        "Runs every 24 hours (UTC), first run now."
    once = {"source_id": "schedule", "source_version": 2, "config": {"kind": "once", "at": "2026-12-24T18:30"}}
    p = gw.post("/api/gateway/automations/schedule-preview", headers=HEADERS, json={"trigger": once}).json()
    assert p["first_run_sentence"] == "Runs once at Thu 24 Dec 18:30 (Europe/Paris)."
    assert p["next_run_at"] == "2026-12-24T17:30:00+00:00"
    bad = {"source_id": "schedule", "source_version": 2, "config": {"kind": "monthly", "day": 40, "at": "08:00"}}
    r = gw.post("/api/gateway/automations/schedule-preview", headers=HEADERS, json={"trigger": bad})
    assert r.status_code == 422 and r.json()["detail"]["field"] == "trigger.config.day"
    assert gw.get("/api/gateway/automations", headers=HEADERS).json()["items"] == []  # nothing stored


# ------------------------------------------------------------------ wording


NOW = datetime.datetime(2026, 10, 8, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize(
    "trigger, zone, expected",
    [
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "daily", "at": "08:00"}}, "Europe/Paris", "Every day at 08:00 (Europe/Paris)"),
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "weekly", "days": ["mon", "wed", "fri"], "at": "07:30"}}, "Europe/Paris", "Every Mon, Wed and Fri at 07:30 (Europe/Paris)"),
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "weekly", "days": ["sun"], "at": "07:30"}}, "UTC", "Every Sun at 07:30 (UTC)"),
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "monthly", "day": 31, "at": "08:00"}}, "Europe/Paris", "Monthly on day 31 (or the last day) at 08:00 (Europe/Paris)"),
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "monthly", "day": 15, "at": "08:00"}}, "Europe/Paris", "Monthly on day 15 at 08:00 (Europe/Paris)"),
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "monthly", "day": "last", "at": "23:00"}}, "UTC", "Monthly on the last day at 23:00 (UTC)"),
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "every", "every": "8h", "count": 3}}, "UTC", "Every 8 hours (UTC) · 3 runs max"),
        ({"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}}, "UTC", "Every hour (UTC)"),
        # A fixed UTC interval names the zone of any clock time it shows (adversary S1).
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "every", "every": "8h", "until": "2026-10-20T06:00:00+00:00"}}, "Europe/Paris", "Every 8 hours (UTC) · until Tue 20 Oct 08:00 (Europe/Paris)"),
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "every", "every": "8h", "until": "2026-10-20T06:00:00+00:00"}}, "UTC", "Every 8 hours (UTC) · until Tue 20 Oct 06:00"),
        ({"source_id": "schedule", "source_version": 2, "config": {"kind": "daily", "at": "08:00", "until": "2026-10-20T06:00:00+00:00"}}, "Europe/Paris", "Every day at 08:00 (Europe/Paris) · until Tue 20 Oct 08:00"),
        ({"source_id": "schedule", "source_version": 1, "config": {"start_at": "2026-10-09T06:00:00+00:00"}}, "Europe/Paris", "Once at Fri 9 Oct 08:00 (Europe/Paris)"),
        ({"source_id": "email.received", "source_version": 1, "config": {}}, "UTC", "When an email arrives"),
        ({"source_id": "manual", "source_version": 1, "config": {}}, "UTC", "Manual runs only"),
    ],
)
def test_rule_text(trigger, zone, expected):
    from abstractgateway.automation_schedule import rule_text

    assert rule_text(trigger, zone, now=NOW) == expected


def test_local_short_adds_the_year_only_when_it_differs():
    from abstractgateway.automation_schedule import local_short

    assert local_short("2026-10-09T06:00:00+00:00", "Europe/Paris", now=NOW) == "Fri 9 Oct 08:00"
    assert local_short("2027-01-04T06:00:00+00:00", "Europe/Paris", now=NOW) == "Mon 4 Jan 2027 07:00"


def test_interval_next_part_names_its_zone_and_the_occurrence_line_shares_the_casing():
    from abstractgateway.automation_schedule import first_run_sentence, schedule_fields
    from abstractgateway.routes.automations import _trigger_summary

    every = {"source_id": "schedule", "source_version": 2, "config": {"kind": "every", "every": "8h", "time_zone": "Europe/Paris"}}
    f = schedule_fields(every, "2026-10-09T06:00:00+00:00", "Asia/Tokyo", now=NOW)
    assert f["time_zone"] == "Europe/Paris"
    assert f["schedule_text"] == "Every 8 hours (UTC) · next Fri 9 Oct 08:00 (Europe/Paris)"
    assert first_run_sentence(every, "Europe/Paris", "2026-10-09T06:00:00+00:00", now=NOW) == \
        "Runs every 8 hours (UTC), first run Fri 9 Oct 08:00 (Europe/Paris)."
    v1 = {"source_id": "schedule", "source_version": 1, "config": {"every": "30m"}}
    assert _trigger_summary({"source_id": "schedule", "payload": {"tick": 5}}, v1) == "schedule: Every 30 minutes (UTC), tick 5"
