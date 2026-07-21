"""Board metadata in backlog list summaries (continuum c1087 ask 1).

The board renders priority/label chips; parsing them at LIST time from the
`> Priority:` / `> Labels:` header conventions kills the client-side N+1
(continuum fetched 40 item bodies per refresh to get these two fields).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _item_md(item_id: int, *, priority: str = "", labels: str = "", title: str = "Do the thing") -> str:
    lines = [
        f"# {item_id:03d}-abstractgateway: {title}",
        "",
        "> Created: 2026-07-12",
        "> Type: task",
    ]
    if priority:
        lines.append(f"> Priority: {priority}")
    if labels:
        lines.append(f"> Labels: {labels}")
    lines += ["", "## Summary", "", "A summary line.", ""]
    return "\n".join(lines)


def test_parser_surfaces_priority_and_labels(tmp_path: Path) -> None:
    from abstractgateway.maintenance.backlog_parser import parse_backlog_item

    p = tmp_path / "007_abstractgateway_thing.md"
    p.write_text(_item_md(7, priority="p1", labels="ui, security, sprint-29"), encoding="utf-8")
    item = parse_backlog_item(p, kind="planned")
    assert item is not None
    assert item.priority == "P1", "priority normalizes to uppercase"
    assert item.labels == ("ui", "security", "sprint-29")

    # Undeclared = empty, never a sentinel (V5 absent-key rule).
    p2 = tmp_path / "008_abstractgateway_other.md"
    p2.write_text(_item_md(8), encoding="utf-8")
    item2 = parse_backlog_item(p2, kind="planned")
    assert item2.priority == "" and item2.labels == ()

    # Garbage priority (not P0-P3) is ignored, not propagated.
    p3 = tmp_path / "009_abstractgateway_bad.md"
    p3.write_text(_item_md(9, priority="urgent!!"), encoding="utf-8")
    item3 = parse_backlog_item(p3, kind="planned")
    assert item3 is not None and item3.priority == ""


def test_parser_pins_the_c1088_grammar(tmp_path: Path) -> None:
    """continuum's c1088 grammar precisions: labels normalize (trim,
    lowercase, spaces->hyphens, 40-char/12-label caps); priority tolerates
    trailing prose; metadata parses from the HEADER BLOCK only (before the
    first '## ' section) so body prose can never inject board chips."""
    from abstractgateway.maintenance.backlog_parser import parse_backlog_item

    p = tmp_path / "012_abstractgateway_grammar.md"
    p.write_text(
        "\n".join(
            [
                "# 012-abstractgateway: Grammar case",
                "",
                "> Type: task",
                "> Priority: P2  (raised after triage)",
                "> Labels: UI Polish,  Security , sprint-29, " + "x" * 60 + ", " + ", ".join(f"l{i}" for i in range(20)),
                "",
                "## Summary",
                "",
                "> Priority: P0",  # body lines never override header metadata
                "> Labels: injected",
                "",
            ]
        ),
        encoding="utf-8",
    )
    item = parse_backlog_item(p, kind="planned")
    assert item is not None
    assert item.priority == "P2", "trailing prose after the value is tolerated; body P0 ignored"
    assert item.labels[0] == "ui-polish", "lowercase + spaces->hyphens"
    assert item.labels[1] == "security"
    assert item.labels[2] == "sprint-29"
    assert all(len(l) <= 40 for l in item.labels), "40-char label cap"
    assert len(item.labels) <= 12, "12-label cap"
    assert "injected" not in item.labels, "body lines never inject board chips"


def test_backlog_list_serves_board_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    repo_root = tmp_path / "repo"
    planned = repo_root / "docs" / "backlog" / "planned"
    planned.mkdir(parents=True, exist_ok=True)
    (planned / "010_abstractgateway_board.md").write_text(
        _item_md(10, priority="P2", labels="board, sprint-3"), encoding="utf-8"
    )
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    with TestClient(app) as client:
        r = client.get("/api/gateway/backlog/planned")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        row = next(i for i in items if i["item_id"] == 10)
        assert row["priority"] == "P2"
        assert row["labels"] == ["board", "sprint-3"]


def _exec_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> "FastAPI":
    import abstractgateway.routes.gateway as gateway_routes
    from dataclasses import dataclass

    repo_root = tmp_path / "repo"
    planned = repo_root / "docs" / "backlog" / "planned"
    planned.mkdir(parents=True, exist_ok=True)
    (planned / "011_abstractgateway_exec.md").write_text(_item_md(11, priority="P1"), encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    @dataclass
    class _Stores:
        base_dir: Path

    @dataclass
    class _Service:
        stores: _Stores

    gw_base = tmp_path / "gw"
    gw_base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gateway_routes, "get_gateway_service", lambda: _Service(stores=_Stores(base_dir=gw_base)))

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


def test_execute_target_override_is_operator_gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """continuum c1087 ask 2: per-task model override refuses loudly without
    an operator-declared allowed set; validates against it when declared;
    invalid efforts 400. Defaults untouched when no override is sent."""
    monkeypatch.delenv("ABSTRACTGATEWAY_BACKLOG_EXEC_ALLOWED_MODELS", raising=False)
    app = _exec_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        # dor=skip: DoR default-ON since c3546 (2026-07-20); this test owns the
        # target-override gate, not readiness — bypass explicitly.
        url = "/api/gateway/backlog/planned/011_abstractgateway_exec.md/execute?dor=skip"

        # No allowed set declared: override refused loudly, naming the knob.
        r = client.post(url + "&target_model=gpt-5.2-pro")
        assert r.status_code == 403, r.text
        assert "ABSTRACTGATEWAY_BACKLOG_EXEC_ALLOWED_MODELS" in r.json()["detail"]

        # Declared set: an off-list model still refuses; an on-list one lands.
        monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_EXEC_ALLOWED_MODELS", "gpt-5.2, gpt-5.2-pro")
        r2 = client.post(url + "&target_model=some-other-model")
        assert r2.status_code == 403, r2.text

        # Invalid effort: 400 naming the enum (checked before anything queues).
        r4 = client.post(url + "&target_reasoning_effort=ultra")
        assert r4.status_code == 400, r4.text

        r3 = client.post(url + "&target_model=gpt-5.2-pro&target_reasoning_effort=high")
        assert r3.status_code == 200, r3.text
        rid = r3.json()["request_id"]
        import json as _json

        queued = _json.loads((tmp_path / "gw" / "backlog_exec_queue" / f"{rid}.json").read_text(encoding="utf-8"))
        assert queued["target_model"] == "gpt-5.2-pro"
        assert queued["target_agent"] == "codex:gpt-5.2-pro"
        assert queued["target_reasoning_effort"] == "high"
