"""The 2026-11-07 incident, replayed, and the rule that removes it (operator, 2026-11-28: "IF the
workflow exists, independently of the running folder, it MUST use it. PERIOD.").

That day the live gateway's data folder stayed the same while its code moved: the editable install
was pointed at other copies of the code, and the workflow folder (derived from the location of
`abstractgateway/config.py`) moved with the code, to a `flows/bundles` holding only the tracked,
shipped files. Automations pinned to versions published into the main checkout's folder
(`abstractassistant-orchestrator@0.0.17`, `@0.0.9`, and `@0.0.16` in a user's plane at 17:27) died
with "Workflow '…' not found in registry". Nothing had been deleted.

The rule now: a start serves its `--flows-dir`, else the folder its data folder remembers, else the
folder next to the running code; every service build remembers the folder it serves. The retired
flows-dir variables are read by nothing.

Here the code really runs from two trees (a subprocess whose PYTHONPATH names a copy of
`src/abstractgateway` inside each tree, tests/workflow_folder_fixtures.py), on ONE data folder:

- tree A, a checkout whose `flows/bundles` holds the shipped files plus one published version
  (`demo-flow@0.0.7`); an automation pins it and runs once;
- tree B, the same code, `flows/bundles` holding only the tracked shape (38 files), started with
  NO flag: it serves tree A's folder (remembered), so the same `id@version` set resolves with the
  same bytes, the automation's next occurrence starts on `demo-flow@0.0.7`, and nothing was copied.
"""

from __future__ import annotations

from pathlib import Path


from workflow_folder_fixtures import (
    PUBLISHED_REF,
    PUBLISHED_WORKFLOW,
    RETIRED_FLOWS_VARS,
    automation_body,
    checkout_tree,
    data_dir_flow_files,
    flow_files,
    marker_folder,
    repo_shipped_names,
    sha256,
    start,
    write_published_bundle,
)

REG = [{"op": "registry", "save": "registry"}]


def _occurrence(st, key: str) -> dict:
    out = st.ops[key]
    assert out.get("summary"), out
    return out


def test_a_published_version_survives_the_gateway_running_from_another_tree(tmp_path: Path) -> None:
    published = write_published_bundle(tmp_path / "staging")
    a = checkout_tree(tmp_path / "A", extra=[published])
    data = tmp_path / "data" / "runtime"

    first = start(a, data, ops=[
        {"op": "registry", "save": "registry"},
        {"op": "create_automation", "save": "automation", "body": automation_body("pinned", "Pinned to a published version")},
        {"op": "run_now", "save": "run", "automation_from": "automation", "command_id": "one", "index": 1},
    ])
    assert first.ok, first.why()
    run1 = _occurrence(first, "run")
    assert run1["summary"]["last_occurrence"]["status"] == "completed", run1
    assert str(run1["workflow_id"]).startswith(PUBLISHED_REF), run1
    aid = first.ops["automation"]["automation_id"]
    reg_a = first.ops["registry"]
    assert reg_a[PUBLISHED_REF]["folder"] == str(a.code_folder.resolve())
    assert reg_a[PUBLISHED_REF]["sha256"] == sha256(published)
    data_files_before = data_dir_flow_files(data)

    b = checkout_tree(tmp_path / "B")  # same code, only what the repository ships
    assert len(repo_shipped_names()) == 38 and len(flow_files(b.code_folder)) == 38
    b_before = flow_files(b.code_folder)
    second = start(b, data, ops=[
        {"op": "registry", "save": "registry"},
        {"op": "run_now", "save": "run", "automation_id": aid, "command_id": "two", "index": 2},
    ])
    assert second.ok, second.why()
    reg_b = second.ops["registry"]

    # R1: the same id@version set, the same bytes; the published version read where it was recorded.
    assert {k: v["sha256"] for k, v in reg_b.items()} == {k: v["sha256"] for k, v in reg_a.items()}, (
        sorted(set(reg_a) - set(reg_b)), sorted(set(reg_b) - set(reg_a)))
    assert reg_b[PUBLISHED_REF]["folder"] == str(a.code_folder.resolve())
    # The automation's next occurrence starts on its pin.
    run2 = _occurrence(second, "run")
    assert run2["summary"]["last_occurrence"]["index"] == 2 and run2["summary"]["last_occurrence"]["status"] == "completed", run2
    assert str(run2["workflow_id"]).startswith(PUBLISHED_REF), run2
    assert run2["summary"]["status"] == "active", run2["summary"]

    # Nothing was copied: tree B's folder and the data folder hold no new workflow file (T7).
    assert flow_files(b.code_folder) == b_before
    assert data_dir_flow_files(data) == data_files_before
    assert not any("demo-flow" in k for k in data_dir_flow_files(data)), data_dir_flow_files(data)


def test_a_user_plane_automation_on_a_shared_version_survives_the_other_tree(tmp_path: Path) -> None:
    """The fourth failure (17:27:56, plane `users/default/laurent`): with user accounts on, a user's
    automation pinned to a version that lives in the SHARED folder."""
    published = write_published_bundle(tmp_path / "staging")
    a = checkout_tree(tmp_path / "A", extra=[published])
    data = tmp_path / "data" / "runtime"

    first = start(a, data, user_auth=True, users=["alice"], ops=[
        {"op": "create_automation", "user": "alice", "save": "automation", "body": automation_body("mine", "Alice's pinned automation")},
        {"op": "run_now", "user": "alice", "save": "run", "automation_from": "automation", "command_id": "one", "index": 1},
        {"op": "registry", "user": "alice", "save": "registry"},
    ])
    assert first.ok, first.why()
    run1 = _occurrence(first, "run")
    assert run1["summary"]["last_occurrence"]["status"] == "completed", run1
    assert str(run1["workflow_id"]).startswith(PUBLISHED_REF), run1
    aid = first.ops["automation"]["automation_id"]
    assert first.ops["registry"][PUBLISHED_REF]["folder"] == str(a.code_folder.resolve())
    alice_flows = data / "users" / "default" / "alice" / "flows"
    alice_before = flow_files(alice_flows)

    b = checkout_tree(tmp_path / "B")
    b_before = flow_files(b.code_folder)
    second = start(b, data, user_auth=True, tokens=first.results["tokens"], ops=[
        {"op": "resolves", "user": "alice", "save": "resolves", "workflow_ids": [PUBLISHED_WORKFLOW]},
        {"op": "run_now", "user": "alice", "save": "run", "automation_id": aid, "command_id": "two", "index": 2},
        {"op": "registry", "user": "alice", "save": "registry"},
    ])
    assert second.ok, second.why()
    assert second.ops["resolves"] == {PUBLISHED_WORKFLOW: True}
    run2 = _occurrence(second, "run")
    assert run2["summary"]["last_occurrence"]["index"] == 2 and run2["summary"]["last_occurrence"]["status"] == "completed", run2
    assert str(run2["workflow_id"]).startswith(PUBLISHED_REF), run2
    assert second.ops["registry"][PUBLISHED_REF]["sha256"] == sha256(published)
    assert second.ops["registry"][PUBLISHED_REF]["folder"] == str(a.code_folder.resolve())

    # Nothing copied into the user's own folder or tree B's folder.
    assert flow_files(alice_flows) == alice_before
    assert flow_files(b.code_folder) == b_before


def _folder_of(st, ref: str):
    entry = st.ops["registry"].get(ref)
    return entry["folder"] if entry else None


def test_serve_flows_dir_is_served_and_then_remembered_without_the_flag(tmp_path: Path) -> None:
    """`serve --flows-dir X` serves X; a later start on the same data folder without the flag, from
    other code, serves X too."""
    x = marker_folder(tmp_path / "chosen folder", "marker-x")
    c1 = checkout_tree(tmp_path / "C1")
    data = tmp_path / "data" / "runtime"
    first = start(c1, data, flows_dir=x, runner=False, ops=REG)
    assert first.ok, first.why()
    assert _folder_of(first, "marker-x@1.0.0") == str(x)
    assert _folder_of(first, "basic-agent@0.0.5") == str(x)

    c2 = checkout_tree(tmp_path / "C2")
    c2_before = flow_files(c2.code_folder)
    later = start(c2, data, runner=False, ops=REG)
    assert later.ok, later.why()
    assert _folder_of(later, "marker-x@1.0.0") == str(x), later.ops["registry"].get("marker-x@1.0.0")
    assert {v["folder"] for v in later.ops["registry"].values()} == {str(x)}
    assert flow_files(c2.code_folder) == c2_before


def test_a_spawned_serve_ignores_the_retired_flows_variables(tmp_path: Path) -> None:
    """The three retired variables, set in a spawned `serve`'s environment, change nothing: a fresh
    data folder serves the folder next to the running code, and remembers that one."""
    v = marker_folder(tmp_path / "named by a variable", "marker-v")
    c = checkout_tree(tmp_path / "C")
    data = tmp_path / "data" / "runtime"
    st = start(c, data, runner=False, ops=REG, extra_env={name: str(v) for name in RETIRED_FLOWS_VARS})
    assert st.ok, st.why()
    assert "marker-v@1.0.0" not in st.ops["registry"], st.ops["registry"].get("marker-v@1.0.0")
    assert {e["folder"] for e in st.ops["registry"].values()} == {str(c.code_folder.resolve())}
    later = start(c, data, runner=False, ops=REG)  # what the data folder remembered is the code's folder
    assert later.ok and "marker-v@1.0.0" not in later.ops["registry"], later.why()
    assert {e["folder"] for e in later.ops["registry"].values()} == {str(c.code_folder.resolve())}
