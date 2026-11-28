"""An occurrence row lists only the person's files (automations wave 2026-11-27, spec F "Files").

The operator saw three ids under a run (`62736d734fb3a58eefd061cd82b34f5d application/json`):
storage offloads the runtime wrote for the run (its scratchpad, a sub-run's state, a trace copy).
"""

from __future__ import annotations

from pathlib import Path

from abstractruntime.core.models import RunState, RunStatus
from abstractruntime.storage.artifacts import FileArtifactStore, artifact_ref

from abstractgateway.automation_files import person_files


def _run(vars_, output, run_id="occ-1"):
    return RunState(run_id=run_id, workflow_id="w", status=RunStatus.COMPLETED, current_node="end", vars=vars_, output=output)


def _store(tmp_path: Path) -> FileArtifactStore:
    return FileArtifactStore(tmp_path / "artifacts")


def test_a_run_with_only_storage_offloads_lists_no_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scratch = store.store_json({"cycles": []}, run_id="occ-1", tags={"source": "run_store_offload"})
    trace = store.store_json({"messages": []}, run_id="occ-1", tags={"source": "node_trace_offload"})
    sub = store.store_json({"state": 1}, run_id="occ-1", tags={"source": "run_store_offload"})
    snap = store.store_json({"flow": 1}, run_id="occ-1", tags={"kind": "workflow_snapshot"})
    ev = store.store_json({"tool": "x"}, run_id="occ-1", tags={"kind": "evidence", "tool": "x"})
    # Even placed where a file would be, an offload or a record is never a person's file.
    run = _run({"_runtime": {"scratchpad": artifact_ref(scratch.artifact_id)}, "files": [artifact_ref(snap.artifact_id)],
                "attachments": [artifact_ref(ev.artifact_id)]},
               {"answer": "done", "scratchpad": artifact_ref(trace.artifact_id), "artifacts": [artifact_ref(sub.artifact_id)]})
    assert person_files(run, store) == []


def test_a_file_in_the_output_is_listed_by_its_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chart = store.store(b"\x89PNG", content_type="image/png", run_id="occ-1", tags={"filename": "prices.png"})
    unnamed = store.store(b"a,b", content_type="text/csv", run_id="occ-1", tags={})
    run = _run({}, {"answer": "see the chart", "files": [artifact_ref(chart.artifact_id), artifact_ref(unnamed.artifact_id)]})
    assert person_files(run, store) == [
        {"artifact_id": chart.artifact_id, "name": "prices.png", "mime_type": "image/png",
         "url": f"/api/gateway/runs/occ-1/artifacts/{chart.artifact_id}/content"},
        {"artifact_id": unnamed.artifact_id, "name": "file-2.csv", "mime_type": "text/csv",
         "url": f"/api/gateway/runs/occ-1/artifacts/{unnamed.artifact_id}/content"},
    ]


def test_a_trigger_attachment_is_listed_and_conversation_context_is_not(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pdf = store.store(b"%PDF", content_type="application/pdf", run_id="occ-1", tags={"kind": "attachment", "filename": "invoice.pdf"})
    old = store.store(b"x", content_type="text/plain", run_id="older", tags={"kind": "attachment", "filename": "old.txt"})
    run = _run({"prompt": "file it", "attachments": [artifact_ref(pdf.artifact_id)],
                "context": {"attachments": [artifact_ref(old.artifact_id)]}}, {"answer": "filed"})
    assert [f["name"] for f in person_files(run, store)] == ["invoice.pdf"]


def test_an_output_offloaded_whole_is_walked_and_a_sub_run_file_is_served_from_its_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    made = store.store(b"data", content_type="text/plain", run_id="sub-run-7", tags={"filename": "report.txt"})
    whole = store.store_json({"answer": "x" * 10, "files": [artifact_ref(made.artifact_id)]}, run_id="occ-1",
                             tags={"source": "run_output_offload"})
    run = _run({}, artifact_ref(whole.artifact_id))
    assert person_files(run, store) == [{
        "artifact_id": made.artifact_id, "name": "report.txt", "mime_type": "text/plain",
        "url": f"/api/gateway/runs/sub-run-7/artifacts/{made.artifact_id}/content"}]
