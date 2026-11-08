"""No gateway text cap refuses a title (root ADR 0054, operator 2026-11-08; the runtime removed
its own stored-text caps the same day, 79e0379).

- a 300-character `automation_defaults.title` round-trips through the visual-flow API whole
  (was refused past 120);
- a 500-character published event title is accepted and stored whole (was refused past 200);
- the tray's Sessions row keeps its 60-character DISPLAY fold, labelled with "…", and the whole
  first line rides the same row as `title_whole` (a fold is never a silent cut).
Red against the capped code: 422 / ValueError / no `title_whole`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env
from test_flow_automation_defaults import DEFAULTS, _flow

pytestmark = pytest.mark.basic


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


def test_a_300_char_automation_title_round_trips_whole(client: TestClient) -> None:
    title = ("Watch the three vector stores we evaluated last quarter and tell me which one to keep " * 4).strip()
    assert len(title) >= 300
    defaults = {**DEFAULTS, "title": "  " + title + "  "}  # trimmed, never cut
    created = client.post("/api/gateway/visualflows", headers=HEADERS, json=_flow(automation_defaults=defaults))
    assert created.status_code == 200, created.text
    assert created.json()["automation_defaults"]["title"] == title
    flow_id = created.json()["id"]
    assert client.get(f"/api/gateway/visualflows/{flow_id}", headers=HEADERS).json()["automation_defaults"]["title"] == title
    # The only rules left: a string, non-empty after trimming.
    from abstractgateway.automation_defaults import AutomationDefaultsError, validate_flow_automation_defaults

    assert validate_flow_automation_defaults({**DEFAULTS, "title": "x" * 10_000})["title"] == "x" * 10_000
    assert "title" not in validate_flow_automation_defaults({**DEFAULTS, "title": "   "})
    with pytest.raises(AutomationDefaultsError, match="string"):
        validate_flow_automation_defaults({**DEFAULTS, "title": 12})


def test_a_500_char_published_title_is_stored_whole(tmp_path: Path) -> None:
    from abstractgateway.events_stream import record_published, stream_for

    stream = stream_for(tmp_path, "default", "alice")
    title = ("A very long published title " * 20).strip()  # trimmed at the edges, never cut
    assert len(title) >= 500
    out = record_published(stream, run_id="r1", step_id="s1", payload={"channel": "me", "title": "  " + title + "  "})
    assert out["event_id"]
    stored = [e for e in stream.read(after=0, limit=50) if e.get("id") == out["event_id"] or e.get("event_id") == out["event_id"]]
    assert stored, stream.read(after=0, limit=50)
    assert stored[0]["data"]["title"] == title, "stored whole, not cut, not refused"
    # Non-empty text is the one rule that stays.
    with pytest.raises(ValueError, match="non-empty"):
        record_published(stream, run_id="r1", step_id="s2", payload={"channel": "me", "title": "   "})
    with pytest.raises(ValueError, match="non-empty"):
        record_published(stream, run_id="r1", step_id="s3", payload={"channel": "me", "title": 7})


def test_the_sessions_row_fold_is_labelled_and_the_whole_title_rides_the_row() -> None:
    from abstractgateway.admin_runtimes import TITLE_MAX, _first_line, run_title

    class _Run:
        vars = {"prompt": "Plan the migration of every note, email and transcript into the new store before Friday\nmore"}

    folded, source, whole = run_title(_Run(), {"run_id": "r", "workflow_id": "wf:x"}, store=None, controllers={})
    assert source == "prompt"
    assert folded.endswith("…") and len(folded) == TITLE_MAX
    assert whole == "Plan the migration of every note, email and transcript into the new store before Friday"
    assert folded[:-1] == whole[: TITLE_MAX - 1].rstrip()
    assert _first_line("short", TITLE_MAX) == "short"
