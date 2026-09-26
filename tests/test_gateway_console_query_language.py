"""The console query language: substring by default, glob on `*` / `?`.

Operator 2026-08-20: typing `*.jpg` into a runtime tab's search box found
nothing — every console filter was a plain substring, so the `*` was
compared literally and no row could ever carry it.

The rule now, in ALL of the consoles' search boxes:

* no `*` and no `?` — case-insensitive SUBSTRING (unchanged)
* any `*` or `?`    — case-insensitive GLOB anchored to the WHOLE value,
  then retried against the value's basename

The matcher is transcribed into two other languages — the web console's
JS (`console.py`, `makeNeedle`) and the console-TUI's Rust
(`console-tui/src/query.rs`) — because the Runs and Artifacts tabs filter
server-side while Cache and Logs filter client-side. These tests pin the
Python original; the Rust copy has the same cases in its own suite.
"""

from __future__ import annotations

import fnmatch
import random

import pytest
from node_requirement import require_node
from test_gateway_runs_list_endpoint import _write_min_bundle

from abstractgateway.routes.gateway import (
    ArtifactListItem,
    _artifact_text_matches,
    _glob_matches,
    _query_is_glob,
    _query_value_matches,
)


# --------------------------------------------------------------------------
# The matcher itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, glob",
    [
        ("*.jpg", True),
        ("photo?.png", True),
        ("*", True),
        ("photo.jpg", False),
        ("06-13", False),
        ("", False),
        # `[` is LITERAL — only `*` and `?` switch the language.
        ("run[1]", False),
    ],
)
def test_only_star_and_question_mark_turn_a_query_into_a_glob(query, glob):
    assert _query_is_glob(query) is glob


@pytest.mark.parametrize(
    "value, pattern, want",
    [
        ("photo.jpg", "*.jpg", True),
        ("photo.png", "*.jpg", False),
        # `*` crosses `/` on purpose: these boxes filter a FLAT row list.
        ("/data/runs/r1/out/photo.jpg", "*.jpg", True),
        # Anchored at BOTH ends — a glob is not a substring search.
        ("run-abc-123", "run-*", True),
        ("x-run-abc", "run-*", False),
        ("a/b/c.log", "a*c.log", True),
        ("anything", "*", True),
        ("", "*", True),
        # `?` is exactly one character.
        ("photo7.jpg", "photo?.jpg", True),
        ("photo77.jpg", "photo?.jpg", False),
        ("photo.jpg", "photo?.jpg", False),
    ],
)
def test_glob_matches_is_anchored(value, pattern, want):
    assert _glob_matches(value, pattern) is want


def test_glob_matches_agrees_with_fnmatch_on_the_language_it_implements():
    """`*`/`?` patterns must mean what every other tool means by them.

    The matcher is hand-rolled rather than delegated to `fnmatch` so the
    JS and Rust transcriptions have something small to agree with. This
    is the proof that hand-rolling did not invent a dialect.
    """
    random.seed(7)
    alphabet = "ab./"
    for _ in range(20000):
        value = "".join(random.choice(alphabet) for _ in range(random.randint(0, 7)))
        pattern = "".join(
            random.choice(alphabet + "*?") for _ in range(random.randint(0, 5))
        )
        assert _glob_matches(value, pattern) == fnmatch.fnmatchcase(value, pattern), (
            value,
            pattern,
        )


def test_glob_backtracking_terminates_on_the_pathological_pattern():
    """The classic `a*a*a*…` blowup must ANSWER, not hang a request."""
    assert _glob_matches("a" * 64, "a*a*a*a*a*a") is True
    assert _glob_matches("a" * 64, "a*a*a*a*a*b") is False


# --------------------------------------------------------------------------
# One field
# --------------------------------------------------------------------------


def test_a_plain_query_is_still_a_substring():
    # The behaviour every console filter had before this change.
    assert _query_value_matches("2026-06-13T09:00:00", "06-13", is_glob=False)
    assert _query_value_matches("xabcx", "abc", is_glob=False)
    assert not _query_value_matches("xabcx", "abd", is_glob=False)


def test_the_basename_pass_rescues_anchored_patterns_against_stored_paths():
    # `photo?.jpg` cannot match a value that starts with `/`; retrying
    # against the basename is what makes an anchored pattern usable at
    # all against `content_path`.
    assert _query_value_matches(
        "/data/runs/r1/photo7.jpg", "photo?.jpg", is_glob=True
    )
    assert not _query_value_matches(
        "/data/runs/r1/photo77.jpg", "photo?.jpg", is_glob=True
    )
    # Windows separators too — the gateway stores whatever the host wrote.
    assert _query_value_matches(r"C:\logs\gateway.log", "gateway.*", is_glob=True)


def test_case_is_ignored_on_both_halves():
    # Callers lowercase the query; the value is lowercased here.
    assert _query_value_matches("PHOTO.JPG", "*.jpg", is_glob=True)
    assert _query_value_matches("CAFÉ.jpg", "café*", is_glob=True)


def test_an_absent_field_never_matches_not_even_a_bare_star():
    # A row with no filename must not be dragged in by `*.jpg`'s siblings.
    assert not _query_value_matches("", "*", is_glob=True)
    assert not _query_value_matches(None, "*", is_glob=True)


# --------------------------------------------------------------------------
# The artifacts search, which is what the operator was typing into
# --------------------------------------------------------------------------


def _artifact(**kw) -> ArtifactListItem:
    base = {"artifact_id": "art-1", "run_id": "run-1"}
    base.update(kw)
    return ArtifactListItem(**base)


def test_artifact_search_finds_a_jpg_by_filetype():
    """The reported defect, at the layer the console actually calls."""
    jpg = _artifact(filename="screenshot.jpg", content_type="image/jpeg")
    png = _artifact(filename="diagram.png", content_type="image/png")

    assert _artifact_text_matches(jpg, "*.jpg")
    assert not _artifact_text_matches(png, "*.jpg")
    assert _artifact_text_matches(png, "*.png")


def test_artifact_search_globs_the_stored_path_too():
    row = _artifact(
        filename="photo.jpg",
        content_path="/var/lib/gw/runs/run-1/artifacts/photo.jpg",
    )
    assert _artifact_text_matches(row, "*.jpg")
    assert _artifact_text_matches(row, "*/run-1/*")
    assert not _artifact_text_matches(row, "*/run-2/*")


def test_artifact_search_keeps_every_substring_case_it_had():
    row = _artifact(
        artifact_id="art-77",
        created_at="2026-06-13T09:00:00Z",
        filename="report.md",
        tags={"phase": "review"},
    )
    # The date case the search box was fixed for on 2026-08-19.
    assert _artifact_text_matches(row, "06-13")
    assert _artifact_text_matches(row, "art-77")
    assert _artifact_text_matches(row, "review")
    assert _artifact_text_matches(row, "report")
    # An empty query keeps every row.
    assert _artifact_text_matches(row, "")
    assert not _artifact_text_matches(row, "nothing-here")


def test_a_glob_does_not_accidentally_match_on_an_unrelated_field():
    """`*.jpg` must not be rescued by some other field ending in `.jpg`.

    The haystack is wide (ids, dates, tags, envelope values); an anchored
    glob is what keeps that width from turning into false positives.
    """
    row = _artifact(filename="notes.md", session_id="sess-1", modality="text")
    assert not _artifact_text_matches(row, "*.jpg")
    assert _artifact_text_matches(row, "*.md")


# --------------------------------------------------------------------------
# The runs list, the other server-side search box on a runtime
# --------------------------------------------------------------------------


def test_list_runs_query_reads_the_same_glob_language(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/runs?query=` speaks the language the Artifacts box speaks.

    Four tabs, one typed query, one rule — a glob that works on Artifacts
    and dies on Runs is the drift this pins shut. Globbed here on
    `workflow_id`, the one identity field with a shape a human can type.
    """
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-globq", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from fastapi.testclient import TestClient

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={"bundle_id": "bundle-globq", "flow_id": "root", "input_data": {}},
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _ids(query: str) -> list[str]:
            r = client.get(
                "/api/gateway/runs",
                params={"limit": 25, "query": query, "include_ledger_len": "false"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            return [i.get("run_id") for i in (r.json().get("items") or [])]

        # The run's workflow_id is `bundle-globq@0.0.0:root`.
        assert run_id in _ids("bundle-globq@*")
        assert run_id in _ids("*:root")
        assert run_id in _ids("bundle-glob?@0.0.0:root")
        assert run_id in _ids("*globq*root*")
        # A glob is ANCHORED — this is the half a substring search cannot
        # do, and the reason `bundle-globq:*` (no version) finds nothing.
        assert run_id not in _ids("globq@*")
        assert run_id not in _ids("*:leaf")
        assert run_id not in _ids("bundle-globq:*")
        # And the substring half is untouched.
        assert run_id in _ids("globq")
        assert run_id in _ids(run_id[:8])
        assert run_id not in _ids("no-such-workflow")


# --------------------------------------------------------------------------
# Three languages, one rule
# --------------------------------------------------------------------------


def test_the_web_consoles_javascript_matcher_agrees_with_this_one() -> None:
    """The JS transcription must not have invented a dialect.

    Cache and Logs filter in the BROWSER while Runs and Artifacts filter
    here, so a query typed on one tab and re-typed on the next has to mean
    the same thing. `node --check` proves the JS parses; this proves it
    answers identically on the same 20k cases.
    """
    import json
    import re
    import shutil
    import subprocess
    import tempfile

    node = require_node()

    from abstractgateway.console import gateway_console_html

    scripts = re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S)
    assert scripts
    source = "\n".join(scripts)
    assert "function makeNeedle" in source, "the shared matcher left the console"

    # The same generator, seeded the same way, on both sides.
    random.seed(11)
    alphabet = "ab./"
    cases = []
    for _ in range(20000):
        value = "".join(random.choice(alphabet) for _ in range(random.randint(0, 7)))
        query = "".join(
            random.choice(alphabet + "*?") for _ in range(random.randint(0, 5))
        )
        cases.append([value, query])

    harness = """
import vm from "node:vm";
const source = %s;
const cases = %s;
// The console's script expects a DOM at load time; we only want the two
// pure functions, so evaluate in a sandbox and export them explicitly.
const ctx = vm.createContext({ console });
try { vm.runInContext(source, ctx); } catch (e) { /* DOM wiring dies; the functions are hoisted */ }
const out = cases.map(([value, query]) => ctx.makeNeedle(query).matches(value));
console.log(JSON.stringify(out));
""" % (json.dumps(source), json.dumps(cases))

    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8") as f:
        f.write(harness)
        f.flush()
        result = subprocess.run(
            [node, f.name], capture_output=True, text=True, check=False
        )
    assert result.returncode == 0, result.stderr
    js = json.loads(result.stdout.strip().splitlines()[-1])

    py = [
        _query_value_matches(value, query.strip().lower(), is_glob=_query_is_glob(query))
        if query.strip()
        else True
        for value, query in cases
    ]
    disagreements = [
        (cases[i], py[i], js[i]) for i in range(len(cases)) if py[i] != js[i]
    ]
    assert not disagreements, (
        f"{len(disagreements)} of {len(cases)} cases disagree; "
        f"first five: {disagreements[:5]}"
    )


def test_artifacts_search_endpoint_answers_a_filetype_glob(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact surface the operator typed `*.jpg` into, over HTTP.

    `_artifact_text_matches` is unit-tested above; this pins the wiring —
    the console sends `?query=*.jpg` and the endpoint has to answer with
    the row rather than the empty page it used to return.

    Note which field the glob bites on. `content_path` is stamped onto
    SERVED rows only (one store lookup per served row, never per
    candidate), so it is not in the haystack at match time — a filetype
    glob works off `filename` / `source_path`, which is what the console's
    Name column shows anyway.
    """
    from test_gateway_artifacts_endpoint import _write_test_bundle

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)

    workspace = tmp_path / "workspace"
    (workspace / "shots").mkdir(parents=True, exist_ok=True)
    # DISTINCT bytes: the store is content-addressed, so two identical
    # files would collapse into one artifact and this would test nothing.
    (workspace / "shots" / "photo1.jpg").write_bytes(b"\xff\xd8\xff\xdb one")
    (workspace / "shots" / "photo22.jpg").write_bytes(b"\xff\xd8\xff\xdb twentytwo")
    (workspace / "notes.md").write_text("# notes\n", encoding="utf-8")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from fastapi.testclient import TestClient

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:

        def _import(path: str) -> str:
            r = client.post(
                "/api/gateway/artifacts/import",
                json={
                    "session_id": "s-glob",
                    "source": {"kind": "workspace_path", "path": path},
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            return (r.json().get("artifact") or {})["$artifact"]

        jpg1 = _import("shots/photo1.jpg")
        jpg22 = _import("shots/photo22.jpg")
        md = _import("notes.md")

        def _ids(query: str) -> list[str]:
            r = client.get(
                "/api/gateway/artifacts/search",
                params={"scope": "all", "limit": 50, "query": query},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            return [it["artifact_id"] for it in r.json()["items"]]

        # The shape the operator typed. This returned an EMPTY page before.
        jpgs = _ids("*.jpg")
        assert jpg1 in jpgs and jpg22 in jpgs
        assert md not in jpgs
        assert md in _ids("*.md")

        # `?` is exactly one character, and the basename pass is what lets
        # an anchored pattern reach a value stored as `shots/photo1.jpg`.
        assert _ids("photo?.jpg") == [jpg1]
        # `*` crosses `/`, so a directory prefix filters too.
        assert set(_ids("shots/*")) == {jpg1, jpg22}
        assert md not in _ids("shots/*")

        # A glob is ANCHORED, which is the half a substring cannot express:
        # `jpg` finds both files, `jpg*` finds nothing (no value STARTS
        # with it), and `*jpg` is how you say "ends with" on purpose.
        assert jpg1 in _ids("jpg") and jpg22 in _ids("jpg")
        assert not _ids("jpg*")
        assert jpg1 in _ids("*jpg") and jpg22 in _ids("*jpg")

        # And the substring half is untouched.
        assert jpg1 in _ids("photo1.jpg")
        assert jpg1 in _ids(jpg1)
        assert not _ids("no-such-artifact")
