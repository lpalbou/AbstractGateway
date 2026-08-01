"""Model WEIGHTS through the Gateway: availability, download jobs, single-flight.

The capability grid says which model a route uses. This surface says whether
that model's weights are on the execution host, and fetches them when they are
not -- with the SAME vocabulary AbstractCore's CLI and both console-TUIs use
(installed / absent / unknown / not applicable).

Three properties are load-bearing and each is pinned here:

  - THE ARTIFACT IS NOT THE MODEL ID. A route stores `qwen/qwen3.5-9b`; the
    weights that get fetched are `qwen/qwen3.5-9b@4bit`. A console that posted
    the row's `model` would ask LM Studio for whatever quant it prefers.
  - SINGLE-FLIGHT. Two requests for the same artifact must produce ONE
    provider-tool invocation, or two `lms get` processes fight over one file.
  - NOTHING DOWNLOADS BY ITSELF. Reading availability spends no bytes.
"""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.basic


@pytest.fixture(autouse=True)
def _clean_jobs():
    from abstractgateway import model_downloads

    model_downloads.reset_for_tests()
    yield
    model_downloads.reset_for_tests()


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_availability_payload_annotates_the_gateway_resolved_grid(monkeypatch, tmp_path):
    """The annotation must ride the GATEWAY's routes, not a second config read.

    The Gateway resolves routes across install / gateway-runtime / principal
    scopes. Annotating a fresh local read would describe a different store than
    the grid renders -- the availability column and the provider column would
    disagree on exactly the multi-user deployments where it matters.
    """

    from abstractgateway import core_config

    resolved = [
        {"key": "input.text", "provider": "lmstudio", "model": "qwen/qwen3.5-9b", "configured": True},
        {"key": "output.voice", "provider": "supertonic", "model": "supertonic-3", "configured": True},
    ]
    monkeypatch.setattr(
        core_config,
        "gateway_capability_defaults_payload",
        lambda **kw: {"ok": True, "routes": resolved, "source": "abstractcore.gateway_runtime", "seeded": "recommended-v1"},
    )
    seen = {}

    def fake_annotate(rows):
        seen["rows"] = rows
        return [{**row, "availability": {"status": "absent", "downloadable": True}} for row in rows]

    monkeypatch.setattr(core_config.config_facade, "annotate_model_availability", fake_annotate)
    monkeypatch.setattr(
        core_config.config_facade,
        "recommended_model_plan",
        lambda: {"total": 3, "installed": 2, "absent": 1, "unknown": 0, "would_download": [], "recommended": []},
    )
    payload = core_config.gateway_model_availability_payload()
    assert seen["rows"] == resolved
    assert payload["seeded"] == "recommended-v1"
    assert payload["recommended"]["installed"] == 2
    assert [row["availability"]["status"] for row in payload["routes"]] == ["absent", "absent"]


def test_availability_survives_a_failed_probe(monkeypatch):
    """A broken probe degrades the WEIGHTS column, never the grid."""

    from abstractgateway import core_config

    monkeypatch.setattr(
        core_config,
        "gateway_capability_defaults_payload",
        lambda **kw: {"ok": True, "routes": [{"key": "input.text", "provider": "lmstudio", "model": "m"}]},
    )

    def boom(_rows):
        raise RuntimeError("lms exploded")

    monkeypatch.setattr(core_config.config_facade, "annotate_model_availability", boom)
    monkeypatch.setattr(core_config.config_facade, "recommended_model_plan", lambda: {})
    payload = core_config.gateway_model_availability_payload()
    assert payload["routes"][0]["key"] == "input.text"
    assert any("lms exploded" in e for e in payload["errors"])
    assert payload["ok"] is False


# ---------------------------------------------------------------------------
# The job runner
# ---------------------------------------------------------------------------


def test_download_job_runs_off_the_caller_and_reports_progress(monkeypatch):
    from abstractgateway import model_downloads

    released = threading.Event()

    class _Progress:
        def __init__(self, message, percent=None, downloaded_bytes=None, total_bytes=None):
            self.message = message
            self.percent = percent
            self.downloaded_bytes = downloaded_bytes
            self.total_bytes = total_bytes

    def fake_download(provider, artifact, *, progress_cb=None, base_url=None, dry_run=False):
        progress_cb(_Progress("pulling manifest"))
        progress_cb(_Progress("pulling layer", percent=50.0, downloaded_bytes=5, total_bytes=10))
        progress_cb(_Progress("pulling layer", percent=51.0))
        released.wait(5)
        return {"provider": provider, "artifact": artifact, "ok": True, "status": "completed", "message": "pulled"}

    monkeypatch.setattr(model_downloads, "core_model_download", fake_download)

    started = model_downloads.start_download("ollama", "tiny:1b")
    assert started["status"] == "running"
    # The POST returned before the download finished -- that is the whole point.
    assert started["job"]

    for _ in range(100):
        job = model_downloads.get_job(started["job"])
        if job["percent"] is not None:
            break
        time.sleep(0.02)
    job = model_downloads.get_job(started["job"])
    assert job["percent"] in (50.0, 51.0)
    assert job["downloaded_bytes"] == 5 and job["total_bytes"] == 10
    # A byte counter that repeats its message must not repeat in the buffer.
    assert job["events"] == ["pulling manifest", "pulling layer"]

    released.set()
    for _ in range(200):
        job = model_downloads.get_job(started["job"])
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "completed"
    assert job["result"]["status"] == "completed"
    assert job["percent"] == 100.0


def test_second_request_for_the_same_artifact_joins_the_running_job(monkeypatch):
    from abstractgateway import model_downloads

    calls = []
    released = threading.Event()

    def fake_download(provider, artifact, *, progress_cb=None, base_url=None, dry_run=False):
        calls.append((provider, artifact))
        released.wait(5)
        return {"ok": True, "status": "completed"}

    monkeypatch.setattr(model_downloads, "core_model_download", fake_download)

    first = model_downloads.start_download("lmstudio", "qwen/qwen3.5-9b@4bit")
    second = model_downloads.start_download("lmstudio", "qwen/qwen3.5-9b@4bit")
    assert second["job"] == first["job"], "a duplicate request must JOIN, not start a second download"
    assert second["joined"] == 2
    released.set()
    for _ in range(200):
        if model_downloads.get_job(first["job"])["status"] != "running":
            break
        time.sleep(0.02)
    assert calls == [("lmstudio", "qwen/qwen3.5-9b@4bit")], f"one invocation, got {calls}"


def test_a_finished_job_frees_the_single_flight_slot(monkeypatch):
    """A FAILED pull is worth retrying; the slot must not hold a corpse."""

    from abstractgateway import model_downloads

    calls = []

    def fake_download(provider, artifact, *, progress_cb=None, base_url=None, dry_run=False):
        calls.append(artifact)
        return {"ok": False, "status": "failed", "message": "boom"}

    monkeypatch.setattr(model_downloads, "core_model_download", fake_download)
    first = model_downloads.start_download("ollama", "tiny:1b")
    for _ in range(200):
        if model_downloads.get_job(first["job"])["status"] != "running":
            break
        time.sleep(0.02)
    assert model_downloads.get_job(first["job"])["status"] == "failed"
    second = model_downloads.start_download("ollama", "tiny:1b")
    assert second["job"] != first["job"]
    assert len(calls) == 2


def test_a_crashing_downloader_fails_the_job_instead_of_hanging_it(monkeypatch):
    from abstractgateway import model_downloads

    def fake_download(*a, **kw):
        raise RuntimeError("the tool vanished")

    monkeypatch.setattr(model_downloads, "core_model_download", fake_download)
    job = model_downloads.start_download("ollama", "tiny:1b")
    for _ in range(200):
        snapshot = model_downloads.get_job(job["job"])
        if snapshot["status"] != "running":
            break
        time.sleep(0.02)
    assert snapshot["status"] == "failed"
    assert "the tool vanished" in snapshot["message"]


def test_recommended_action_starts_exactly_the_recommended_artifacts(monkeypatch):
    from abstractgateway import model_downloads

    monkeypatch.setattr(
        model_downloads,
        "recommended_core_model_downloads",
        lambda: [
            {"route": "input.text", "provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit"},
            {"route": "output.voice", "provider": "supertonic", "artifact": "supertonic-3"},
        ],
    )
    asked = []

    def fake_download(provider, artifact, *, progress_cb=None, base_url=None, dry_run=False):
        asked.append((provider, artifact, dry_run))
        return {"ok": True, "status": "planned" if dry_run else "completed"}

    monkeypatch.setattr(model_downloads, "core_model_download", fake_download)
    jobs = model_downloads.start_recommended_downloads(dry_run=True)
    assert len(jobs) == 2
    for _ in range(200):
        if all(model_downloads.get_job(j["job"])["status"] != "running" for j in jobs):
            break
        time.sleep(0.02)
    # THE 4-BIT ARTIFACT, not the served id the route stores.
    assert ("lmstudio", "qwen/qwen3.5-9b@4bit", True) in asked
    assert ("supertonic", "supertonic-3", True) in asked


def test_unknown_job_id_is_absent_not_an_error():
    from abstractgateway import model_downloads

    assert model_downloads.get_job("does-not-exist") is None


def test_start_download_requires_both_parts():
    from abstractgateway import model_downloads

    with pytest.raises(ValueError):
        model_downloads.start_download("", "artifact")
    with pytest.raises(ValueError):
        model_downloads.start_download("ollama", "  ")


# ---------------------------------------------------------------------------
# The console contract
# ---------------------------------------------------------------------------


def test_console_renders_weights_from_the_availability_endpoint():
    """The web console must read the availability surface, not invent one."""

    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert "/api/gateway/models/availability" in html
    assert "/api/gateway/models/download" in html
    # The shared vocabulary, so a row reads the same in the console and the TUIs.
    assert "not downloaded" in html and "installed" in html
    # The banner that makes a fresh install legible.
    assert "defaults-availability" in html
    assert "Download missing" in html
    # A `download_artifact` is what the POST sends -- never the row's model.
    assert "download_artifact" in html
    # An unconfigured route has no weights to be missing; the column stays
    # empty there, exactly as both console-TUIs do.
    assert 'evidence === "route not configured"' in html


def test_single_flight_holds_under_a_concurrent_stampede(monkeypatch):
    """Twenty threads asking at once must still produce ONE provider invocation.

    The sequential test proves the happy path; this one proves the LOCK. Two
    `lms get` processes writing the same files is the failure this guards, and
    it only ever happens under real contention.
    """

    from abstractgateway import model_downloads

    calls = []
    calls_lock = threading.Lock()
    released = threading.Event()

    def fake_download(provider, artifact, *, progress_cb=None, base_url=None, dry_run=False):
        with calls_lock:
            calls.append(artifact)
        released.wait(10)
        return {"ok": True, "status": "completed"}

    monkeypatch.setattr(model_downloads, "core_model_download", fake_download)

    seen = []
    seen_lock = threading.Lock()
    gate = threading.Barrier(20)

    def ask():
        gate.wait(10)
        job = model_downloads.start_download("lmstudio", "qwen/qwen3.5-9b@4bit")
        with seen_lock:
            seen.append(job["job"])

    threads = [threading.Thread(target=ask) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert len(set(seen)) == 1, f"20 concurrent requests produced {len(set(seen))} jobs"
    assert calls == ["qwen/qwen3.5-9b@4bit"], f"one invocation, got {calls}"
    assert model_downloads.get_job(seen[0])["joined"] == 20
    released.set()
    for _ in range(300):
        if model_downloads.get_job(seen[0])["status"] != "running":
            break
        time.sleep(0.02)


def test_distinct_artifacts_download_concurrently_without_crossing_state(monkeypatch):
    """Single-flight is per ARTIFACT, not a global queue.

    Two different models must download at the same time, and neither job may
    inherit the other's progress -- the progress sink is per job id, and a bug
    there would show one download's byte counter on the other's row.
    """

    from abstractgateway import model_downloads

    class _Progress:
        def __init__(self, message, percent=None):
            self.message = message
            self.percent = percent
            self.downloaded_bytes = None
            self.total_bytes = None

    both_running = threading.Barrier(2, timeout=10)
    released = threading.Event()

    def fake_download(provider, artifact, *, progress_cb=None, base_url=None, dry_run=False):
        progress_cb(_Progress(f"pulling {artifact}", percent=10.0 if artifact == "a:1b" else 90.0))
        both_running.wait()  # neither returns until BOTH are in flight
        released.wait(10)
        return {"ok": True, "status": "completed", "message": f"pulled {artifact}"}

    monkeypatch.setattr(model_downloads, "core_model_download", fake_download)

    first = model_downloads.start_download("ollama", "a:1b")
    second = model_downloads.start_download("ollama", "b:1b")
    assert first["job"] != second["job"]

    for _ in range(300):
        a, b = model_downloads.get_job(first["job"]), model_downloads.get_job(second["job"])
        if a["percent"] is not None and b["percent"] is not None:
            break
        time.sleep(0.02)
    a, b = model_downloads.get_job(first["job"]), model_downloads.get_job(second["job"])
    assert (a["percent"], b["percent"]) == (10.0, 90.0), "job progress must not cross"
    assert a["events"] == ["pulling a:1b"] and b["events"] == ["pulling b:1b"]

    released.set()
    for _ in range(300):
        if all(model_downloads.get_job(j["job"])["status"] != "running" for j in (first, second)):
            break
        time.sleep(0.02)
    assert model_downloads.get_job(first["job"])["result"]["message"] == "pulled a:1b"
    assert model_downloads.get_job(second["job"])["result"]["message"] == "pulled b:1b"


def test_a_worker_that_cannot_start_fails_the_job_and_frees_the_slot(monkeypatch):
    """A job whose thread never started would wedge the artifact forever.

    It would sit at `running` holding the single-flight slot, so every later
    request for that artifact would JOIN work that can never finish -- a
    download button that silently stops doing anything until a restart.
    """

    from abstractgateway import model_downloads

    real_thread = threading.Thread

    class _Refuses(real_thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(model_downloads.threading, "Thread", _Refuses)
    job = model_downloads.start_download("ollama", "tiny:1b")
    assert job["status"] == "failed"
    assert "could not start the download worker" in job["message"]

    monkeypatch.setattr(model_downloads.threading, "Thread", real_thread)
    monkeypatch.setattr(
        model_downloads,
        "core_model_download",
        lambda *a, **kw: {"ok": True, "status": "completed"},
    )
    retry = model_downloads.start_download("ollama", "tiny:1b")
    assert retry["job"] != job["job"], "the wedged slot must not survive"


def test_a_restarted_gateway_reports_no_phantom_running_job():
    """Jobs live in this process. A restart must forget them, not fake them.

    A job id that survived a restart as `running` would have a console polling
    a download nothing is doing. 404 is the contract; the console re-reads
    availability, which is the honest answer about the bytes.
    """

    from abstractgateway import model_downloads

    model_downloads._JOBS["ghost"] = model_downloads._Job(
        id="ghost", provider="ollama", artifact="x:1b", dry_run=False
    )
    model_downloads._BY_KEY["ollama/x:1b"] = "ghost"
    assert model_downloads.get_job("ghost")["status"] == "running"

    model_downloads.reset_for_tests()  # what a fresh process looks like
    assert model_downloads.get_job("ghost") is None
    assert model_downloads.list_jobs() == []
    assert model_downloads.active_job_for("ollama", "x:1b") is None
