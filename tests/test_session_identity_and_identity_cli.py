"""A conversation's temporary identity (framework ADR 0052 §9) and the identity/record CLI verbs.

- `POST /runs/start` takes an optional `identity` with a `session_id`, checked by the entity ladder
  (design, voice offered, model downloaded), kept on the SESSION (`session_identities.json` in the
  owner's plane, never an entity home); each run of the session carries it inline
  (`_runtime.identity`) with its ref `session:<sid>#<rev>`; a later turn inherits it, another one
  makes rev + 1; a session that names none records null (no Assistant identity on this gateway): no
  ref, never a borrowed identity. A client-sent `_runtime.identity(_ref)` is dropped.
- The run TTS lanes speak with the run's identity voice when the request names none, with
  `identity_ref` / `X-Identity-Ref`.
- `abstractgateway entity identity get|set` and `entity record` call the routes and print plain words.

Each test goes red without the rule it names. Fakes only.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_BUNDLE_ID, ECHO_FLOW_ID, HEADERS, gateway_env, write_echo_bundle

pytestmark = pytest.mark.basic

pytest.importorskip("abstractavatar")
pytest.importorskip("abstractruntime.identity.digital_identity")

OFFERED_VOICES = {"kokoro": (["af_heart", "am_adam"], ["kokoro-82m", "kokoro-big"])}
IDENTITY = {"avatar": {"style": "watch-lotus", "color": "#3366cc"}, "voice": {"provider": "kokoro", "model": "kokoro-82m", "voice": "am_adam"}}


@pytest.fixture(autouse=True)
def _gateway_knows(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.core_config as core_config
    import abstractgateway.entity_settings_access as esa

    monkeypatch.setattr(esa, "offered_voices", lambda p, m: (*OFFERED_VOICES.get(p, ([], [])), None))
    monkeypatch.setattr(core_config, "model_not_downloaded", lambda p, m: ({"message": f"{m} is not downloaded yet.", "download": {"provider": p, "model": m}} if m == "kokoro-big" else None))


@pytest.fixture()
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = gateway_env(monkeypatch, tmp_path)
    write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.data_dir = data_dir  # type: ignore[attr-defined]
        yield c


def _start(c: TestClient, session_id: Any = "s-1", **extra: Any):
    body: Dict[str, Any] = {"bundle_id": ECHO_BUNDLE_ID, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "hi", "notify": False}, **extra}
    if session_id is not None:
        body["session_id"] = session_id
    return c.post("/api/gateway/runs/start", headers=HEADERS, json=body)


def _vars(run_id: str) -> Dict[str, Any]:
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().host.run_store.load(run_id).vars


def _stored(c: TestClient) -> Dict[str, Any]:
    from abstractgateway.session_identity import FILENAME

    return json.loads((Path(c.data_dir) / FILENAME).read_text())["sessions"]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- the session's identity


def test_a_run_start_identity_is_kept_on_the_session_and_rides_every_turn(gw) -> None:
    r = _start(gw, identity=IDENTITY)
    assert r.status_code == 200, r.text
    first = _vars(r.json()["run_id"])["_runtime"]
    assert first["identity_ref"] == {"of": "session:s-1", "rev": 1}
    assert first["identity"]["voice"]["voice"] == "am_adam" and first["identity"]["avatar"]["source"] == "chosen"
    assert first["identity"]["set_by"] == "person:admin"
    assert _stored(gw)["s-1"]["rev"] == 1
    # Never in an entity home.
    assert not list((Path(gw.data_dir) / "entities").glob("*/identity.json"))  # type: ignore[attr-defined]
    # A later turn inherits it; another identity is rev 2.
    later = _vars(_start(gw).json()["run_id"])["_runtime"]
    assert later["identity_ref"] == {"of": "session:s-1", "rev": 1} and later["identity"] == first["identity"]
    changed = {**IDENTITY, "voice": {"provider": "kokoro", "model": "kokoro-82m", "voice": "af_heart"}}
    third = _vars(_start(gw, identity=changed).json()["run_id"])["_runtime"]
    assert third["identity_ref"] == {"of": "session:s-1", "rev": 2} and third["identity"]["voice"]["voice"] == "af_heart"


def test_the_ladder_refuses_and_nothing_is_kept(gw) -> None:
    bad_style = {**IDENTITY, "avatar": {"style": "iphone-weave", "color": "#3366cc"}}
    r = _start(gw, identity=bad_style)
    assert r.status_code == 400 and "can't be drawn on the Watch" in r.json()["detail"], r.text
    r = _start(gw, identity={**IDENTITY, "voice": {"provider": "kokoro", "model": "kokoro-82m", "voice": "zz"}})
    assert r.status_code == 400 and r.json()["detail"].startswith("This conversation can't speak with zz: it isn't a kokoro voice"), r.text
    r = _start(gw, identity={**IDENTITY, "voice": {"provider": "kokoro", "model": "kokoro-big", "voice": "am_adam"}})
    assert r.status_code == 409 and r.json()["error"]["download"]["model"] == "kokoro-big", r.text
    r = _start(gw, identity={"avatar": IDENTITY["avatar"]})
    assert r.status_code == 400 and "needs a voice" in r.json()["detail"], r.text
    from abstractgateway.session_identity import FILENAME

    assert not (Path(gw.data_dir) / FILENAME).exists()  # type: ignore[attr-defined]


def test_an_identity_needs_a_session(gw) -> None:
    r = _start(gw, session_id=None, identity=IDENTITY)
    assert r.status_code == 400 and "needs a session_id" in r.json()["detail"]


def test_a_session_naming_none_records_null_and_carries_no_ref(gw) -> None:
    """No Assistant identity on this gateway: the default is recorded null, no ref, nothing borrowed;
    and an identity given later still becomes rev 1."""
    run = _vars(_start(gw, session_id="s-2").json()["run_id"])["_runtime"]
    assert "identity" not in run and "identity_ref" not in run
    assert _stored(gw)["s-2"] == {**_stored(gw)["s-2"], "rev": 0, "identity": None, "set_by": "default"}
    assert _vars(_start(gw, session_id="s-2", identity=IDENTITY).json()["run_id"])["_runtime"]["identity_ref"]["rev"] == 1


def test_a_client_sent_identity_or_ref_is_dropped(gw) -> None:
    forged = {"_runtime": {"identity_ref": {"of": "entity:castor", "rev": 9}, "identity": {"rev": 9}}, "prompt": "hi", "notify": False}
    r = gw.post("/api/gateway/runs/start", headers=HEADERS, json={"bundle_id": ECHO_BUNDLE_ID, "flow_id": ECHO_FLOW_ID, "session_id": "s-3", "input_data": forged})
    assert r.status_code == 200, r.text
    rt = _vars(r.json()["run_id"]).get("_runtime") or {}
    assert "identity_ref" not in rt and "identity" not in rt


def test_get_session_identity_resolves_a_session_reference_to_a_look(gw) -> None:
    _start(gw, identity=IDENTITY)
    body = gw.get("/api/gateway/sessions/s-1/identity", headers=HEADERS).json()
    assert body["identity_ref"] == {"of": "session:s-1", "rev": 1}
    assert body["identity"]["rev"] == 1 and body["identity"]["avatar"]["watch_code"] and body["identity"]["voice"]["voice"] == "am_adam"
    _start(gw, identity={**IDENTITY, "avatar": {"style": "watch-halo", "color": "#112233"}})
    assert gw.get("/api/gateway/sessions/s-1/identity", headers=HEADERS).json()["identity_ref"]["rev"] == 2
    first = gw.get("/api/gateway/sessions/s-1/identity?rev=1", headers=HEADERS).json()
    assert first["identity"]["avatar"]["style"] == "watch-lotus" and first["identity_ref"] == {"of": "session:s-1", "rev": 1}
    assert gw.get("/api/gateway/sessions/s-1/identity?rev=9", headers=HEADERS).status_code == 404
    _start(gw, session_id="s-none")
    assert gw.get("/api/gateway/sessions/s-none/identity", headers=HEADERS).json() == {"session_id": "s-none", "identity": None, "identity_ref": None}
    r = gw.get("/api/gateway/sessions/s-nobody/identity", headers=HEADERS)
    assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "session_not_found"


def test_run_rows_read_the_identity_ref_through(gw) -> None:
    with_ref = _start(gw, identity=IDENTITY).json()["run_id"]
    row = gw.get(f"/api/gateway/runs/{with_ref}", headers=HEADERS).json()
    assert row["identity_ref"] == {"of": "session:s-1", "rev": 1}
    without = _start(gw, session_id="s-plain").json()["run_id"]
    assert "identity_ref" not in gw.get(f"/api/gateway/runs/{without}", headers=HEADERS).json()


# --------------------------------------------------------------------------- the run TTS lanes


def test_the_run_tts_stream_speaks_the_session_identity_voice_with_its_ref(gw, monkeypatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes
    from abstractgateway.service import get_gateway_service

    monkeypatch.setattr(gateway_routes, "_configured_voice_output_defaults", lambda kind: {"provider": "fake-tts", "model": "fake-1", "voice": "V9"})
    asked: List[Dict[str, Any]] = []
    wav = b"RIFF" + bytes(60)

    class _FakeSpeech:
        def stream_tts(self, *, text, output=None, params=None):
            asked.append(dict(output or {}))
            yield {"type": "audio", "sequence": 0, "content_type": "audio/wav", "audio": wav, "size_bytes": len(wav)}
            yield {"type": "done", "ok": True, "chunks": 1}

    run_id = _start(gw, identity=IDENTITY).json()["run_id"]
    monkeypatch.setattr(get_gateway_service().host.runtime, "_abstractcore_llm_client", _FakeSpeech(), raising=False)
    resp = gw.post(f"/api/gateway/runs/{run_id}/voice/tts/stream", headers=HEADERS, json={"text": "Read this."})
    assert resp.status_code == 200, resp.text
    assert asked[-1]["provider"] == "kokoro" and asked[-1]["voice"] == "am_adam"  # never the gateway default V9
    assert resp.headers["X-Identity-Ref"] == "session:s-1#1"
    # A request naming its own voice passes untouched, with no ref.
    resp = gw.post(f"/api/gateway/runs/{run_id}/voice/tts/stream", headers=HEADERS, json={"text": "Read this.", "voice": "V9", "provider": "fake-tts", "model": "fake-1"})
    assert asked[-1]["voice"] == "V9" and "X-Identity-Ref" not in resp.headers
    # A run of a session with no identity: today's behaviour (the gateway default), no ref.
    plain = _start(gw, session_id="s-4").json()["run_id"]
    resp = gw.post(f"/api/gateway/runs/{plain}/voice/tts/stream", headers=HEADERS, json={"text": "Read this."})
    assert asked[-1]["voice"] == "V9" and "X-Identity-Ref" not in resp.headers


def test_the_run_tts_lane_answers_the_ref_in_the_body_and_the_header(gw, monkeypatch) -> None:
    from types import SimpleNamespace

    import abstractgateway.routes.gateway as gateway_routes

    asked: List[Dict[str, Any]] = []

    class _Facade:
        def generate_voice(self, run_id, *, text, output, params):
            asked.append(dict(output or {}))
            return SimpleNamespace(run_id="child-1")

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (_Facade(), None))
    monkeypatch.setattr(gateway_routes, "_gateway_completed_child_result", lambda child, operation: {"outputs": {"voice": [{"artifact_id": "a1"}]}})
    monkeypatch.setattr(gateway_routes, "_gateway_generated_artifact_ref_from_item", lambda item, **kw: {"$artifact": "a1"})
    run_id = _start(gw, identity=IDENTITY).json()["run_id"]
    resp = gw.post(f"/api/gateway/runs/{run_id}/voice/tts", headers=HEADERS, json={"text": "Read this."})
    assert resp.status_code == 200, resp.text
    assert asked[-1]["voice"] == "am_adam"
    assert resp.json()["identity_ref"] == {"of": "session:s-1", "rev": 1} and resp.headers["X-Identity-Ref"] == "session:s-1#1"


def test_the_run_identity_voice_rule() -> None:
    from types import SimpleNamespace

    from abstractgateway.routes.gateway import VoiceTTSRequest, _run_identity_voice

    record = {"schema_version": 1, "rev": 2, "avatar": {"style": "watch-lotus", "color": "#3366cc", "source": "chosen"},
              "voice": {"provider": "kokoro", "model": "kokoro-82m", "voice": "am_adam", "speed": 1.2, "source": "chosen"},
              "set_at": "2026-11-04T10:00:00+00:00", "set_by": "person:admin"}
    run = SimpleNamespace(vars={"_runtime": {"identity": record, "identity_ref": {"of": "session:s", "rev": 2}}})
    req, ref = _run_identity_voice(run, VoiceTTSRequest(text="x"))
    assert (req.provider, req.model, req.voice, req.speed) == ("kokoro", "kokoro-82m", "am_adam", 1.2) and str(ref) == "session:s#2"
    req, ref = _run_identity_voice(run, VoiceTTSRequest(text="x", profile="p1"))
    assert req.voice is None and ref is None
    req, ref = _run_identity_voice(SimpleNamespace(vars={}), VoiceTTSRequest(text="x"))
    assert req.voice is None and ref is None


# --------------------------------------------------------------------------- the CLI


class _Transport:
    def __init__(self, c: TestClient) -> None:
        self.c = c
        self.calls: List[tuple] = []

    def call(self, method: str, path: str, body: Any = None, *, timeout: Any = None):
        from abstractgateway.models_engines_cli import _Answer

        self.calls.append((method, path, body))
        r = self.c.request(method, "/api/gateway" + path, headers=HEADERS, json=body)
        try:
            data = r.json()
        except ValueError:
            data = None
        return _Answer(r.status_code, data)


def _cli(argv: List[str]) -> int:
    """The exit code of `abstractgateway <argv>` (0 when it returns normally)."""
    from abstractgateway.cli import main

    try:
        main(argv)
    except SystemExit as done:
        return int(done.code or 0)
    return 0


@pytest.fixture()
def entity_gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import abstractgateway.entity_identity as ei
    import abstractgateway.entity_identity_cli as cli

    pytest.importorskip("abstractmemory")
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    data_dir = gateway_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ei, "gateway_default_voice", lambda data_dir: {"provider": "kokoro", "model": "kokoro-82m", "voice": "af_heart"})
    from abstractgateway.app import app

    with TestClient(app) as c:
        spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
        spark.update({"name": "Castor", "spark": 1})
        assert c.post("/api/gateway/entities", headers=HEADERS, json={"name": "Castor", "spark": spark, "skills": []}).status_code == 201
        transport = _Transport(c)
        monkeypatch.setattr(cli, "_transport", lambda args: transport)
        c.data_dir = data_dir  # type: ignore[attr-defined]
        c.transport = transport  # type: ignore[attr-defined]
        yield c


def test_cli_identity_get_and_set_go_through_the_routes(entity_gw, capsys) -> None:
    assert _cli(["entity", "identity", "set", "castor", "--style", "watch-lotus", "--color", "#3366cc",
                 "--provider", "kokoro", "--model", "kokoro-82m", "--voice", "am_adam"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Saved.") and "watch-lotus in #3366cc" in out and "am_adam  (kokoro / kokoro-82m; chosen)" in out
    assert entity_gw.transport.calls[-1][:2] == ("PUT", "/entities/castor/identity")
    _cli(["entity", "identity", "get", "castor", "--rev", "1"])
    out = capsys.readouterr().out
    assert "entity:castor  (identity entity:castor#1)" in out and "(default; Watch code" in out
    # A refusal is the gateway's sentence on stderr, exit 2.
    code = _cli(["entity", "identity", "set", "castor", "--style", "iphone-weave", "--color", "#3366cc",
                 "--provider", "kokoro", "--model", "kokoro-82m", "--voice", "am_adam"])
    assert code == 2 and "can't be drawn on the Watch" in capsys.readouterr().err


def test_cli_record_reads_the_record_routes(entity_gw, capsys) -> None:
    assert _cli(["entity", "record", "castor"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Track record of entity:castor") and "No experience yet." in out and "completion rate only" in out
    assert entity_gw.transport.calls[-1][:2] == ("GET", "/entities/castor/track-record")
    assert _cli(["entity", "record", "castor", "--reputation"]) == 0
    assert "Reputation of entity:castor: No experience yet" in capsys.readouterr().out
    assert _cli(["entity", "record", "castor", "--acquaintances"]) == 0
    assert "Entities: none yet" in capsys.readouterr().out
    assert _cli(["entity", "record", "castor", "--reputation", "--since", "2026-11-01"]) == 2


def test_the_record_routes_name_an_unreadable_file_without_its_absolute_path(entity_gw) -> None:
    home = Path(entity_gw.data_dir) / "entities" / "castor"  # type: ignore[attr-defined]
    (home / "identity.json").write_text("{not json", encoding="utf-8")
    r = entity_gw.get("/api/gateway/entities/castor/track-record", headers=HEADERS)
    assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "record_unreadable", r.text
    message = r.json()["detail"]["message"]
    assert "castor/identity.json" in message and str(home.parent) not in message, message


def test_cli_record_prints_plain_words(monkeypatch, capsys) -> None:
    """`entity record` renders the routes' payloads (the runtime's to_dict shapes, built here from
    the runtime's own types): "No experience yet" at support 0, the completion-rate basis spelled out."""
    import abstractgateway.entity_identity_cli as cli
    from abstractgateway.models_engines_cli import _Answer
    from abstractruntime.identity.acquaintances import Acquaintance, Acquaintances
    from abstractruntime.identity.track_record import Reputation, TokenCounts, TrackRecord, TrustCell

    empty_record = TrackRecord(subject="entity:castor", identity_ref=None, fingerprint=None, computed_at="2026-11-04T10:00:00+00:00",
                               as_of=None, since=None, sessions=0, runs=0, successes=0, failures=0, cancelled=0, in_progress=0,
                               assessed=0, tokens=TokenCounts(), by_task_kind={}, recent_outcomes=())
    rep = Reputation(subject="entity:castor", identity_ref=None, computed_at="2026-11-04T10:00:00+00:00",
                     overall=TrustCell(0.5, 0, 0, 0, None), by_task_kind={"visit/v": TrustCell(5 / 7, 5, 4, 1, None)}, evidence=())
    acq = Acquaintances(subject="entity:castor", identity_ref=None, computed_at="x", as_of=None,
                        entities=(Acquaintance("entity:pollux", "a", "b", 1, {"visit/v": 1}),), humans=(), not_counted={})
    answers = {"track-record": empty_record.to_dict(), "reputation": rep.to_dict(), "acquaintances": acq.to_dict()}

    class _T:
        def call(self, method, path, body=None, *, timeout=None):
            return _Answer(200, answers[path.rsplit("/", 1)[-1].split("?", 1)[0]])

    monkeypatch.setattr(cli, "_transport", lambda args: _T())
    _cli(["entity", "record", "castor"])
    out = capsys.readouterr().out
    assert "No experience yet." in out
    assert "Success here means the work finished (completion rate only): how good it was is not assessed yet." in out
    _cli(["entity", "record", "castor", "--reputation"])
    out = capsys.readouterr().out
    assert "Reputation of entity:castor: No experience yet" in out and "visit/v: trust 0.71 over 5 outcomes" in out
    assert "completion rate only" in out
    _cli(["entity", "record", "castor", "--acquaintances"])
    out = capsys.readouterr().out
    assert "entity:pollux: 1 interaction, last b" in out and "People: none yet" in out
