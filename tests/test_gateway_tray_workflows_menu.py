"""The tray's Workflows submenu, and the menu it lives in (2026-09-06).

Operator ruling, three parts:

  - ONE door to the console. "Show Activity in Console" and "Open Console"
    opened the same browser at two anchors; a menu that offers the same
    destination twice makes a new user choose between identical things.
  - A **Workflows** submenu: the last 24 hours of runs with a state badge, the
    step count and the duration, with Pause/Resume as the one control under it.
  - No "(needs internet)" / "(on this computer)" suffixes in Help. A menu item
    names what it opens; where it lives is noise the reader steps over.

Pinned here as PURE functions plus the payload folding, so none of it needs a
display: the labels, the tally, the 24 h window, and the menu signature (a run
list that changed must rebuild the menu; a duration that ticks must not).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.basic


def _items(payload: Dict[str, Any], **kw: Any) -> List[Any]:
    from abstractgateway.tray.sampler import run_rows_from_listing

    return run_rows_from_listing(payload, **kw)


# --------------------------------------------------------------- the window


def test_only_the_last_day_survives_but_an_unreadable_stamp_is_kept() -> None:
    now = 1_788_700_000.0
    rows = _items(
        {
            "items": [
                {"run_id": "fresh", "workflow_id": "a:b", "status": "completed", "created_at": now - 3600},
                {"run_id": "stale", "workflow_id": "a:b", "status": "completed", "created_at": now - (26 * 3600)},
                {"run_id": "undated", "workflow_id": "a:b", "status": "running"},
                {"run_id": "", "workflow_id": "a:b", "status": "running"},
                "not a dict",
            ]
        },
        now=now,
    )

    # A run we cannot date is SHOWN: the window is a kindness to the reader,
    # never a reason to lose a run that may be running right now.
    assert [r.run_id for r in rows] == ["fresh", "undated"]


def test_timestamps_are_read_in_every_shape_a_run_store_writes() -> None:
    from abstractgateway.tray.sampler import run_rows_from_listing

    now = time.time()
    rows = run_rows_from_listing(
        {
            "items": [
                {
                    "run_id": "iso-z",
                    "workflow_id": "coding-agent:coder",
                    "status": "completed",
                    "ledger_len": 12,
                    "created_at": "2026-09-06T12:00:00Z",
                    "updated_at": "2026-09-06T12:02:13Z",
                }
            ]
        },
        now=now,
        window_s=float("inf"),
    )
    assert rows[0].duration_s == pytest.approx(133.0)
    assert rows[0].steps == 12

    # An offset and a naive stamp both parse; a broken one is None, not a crash.
    offset = run_rows_from_listing(
        {"items": [{"run_id": "o", "workflow_id": "w", "status": "running", "created_at": "2026-09-06T12:00:00+02:00"}]},
        now=now,
        window_s=float("inf"),
    )
    assert offset[0].started_at is not None
    broken = run_rows_from_listing(
        {"items": [{"run_id": "b", "workflow_id": "w", "status": "running", "created_at": "yesterday-ish"}]},
        now=now,
    )
    assert broken[0].started_at is None

    # A run still going has no end: a duration is never invented for it.
    running = run_rows_from_listing(
        {"items": [{"run_id": "r", "workflow_id": "w", "status": "running", "created_at": "2026-09-06T12:00:00Z"}]},
        now=now,
        window_s=float("inf"),
    )
    assert running[0].duration_s is None


def test_a_listing_that_is_not_one_yields_nothing() -> None:
    assert _items({}) == [] and _items({"items": "nope"}) == [] and _items(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------- the labels


def test_a_run_row_reads_at_a_glance() -> None:
    from abstractgateway.tray.app import RUN_BADGES, run_row_label
    from abstractgateway.tray.sampler import RunRow

    done = RunRow("1", "deep-research:main", "completed", 31, 724.0, 1.0)
    assert run_row_label(done) == "✅ deep-research:main · 31 steps · 12m 04s"

    # A run in flight says its duration is not final.
    live = RunRow("2", "coding-agent:coder", "running", 4, 133.0, 1.0)
    assert run_row_label(live) == "🟢 coding-agent:coder · 4 steps · 2m 13s so far"

    # One step is not "1 steps"; a missing step count is simply absent.
    assert "1 step ·" in run_row_label(RunRow("3", "w", "failed", 1, 8.0, 1.0))
    assert run_row_label(RunRow("4", "w", "failed", None, None, 1.0)) == "❌ w"

    # A long bundle name is the elastic part — the badge and the numbers are
    # what the operator opened the menu for and are never squeezed out.
    long_row = RunRow("5", "some-very-long-bundle-name:with-a-long-flow", "completed", 2, 5.0, 1.0)
    label = run_row_label(long_row)
    assert label.startswith("✅ ") and "…" in label and label.endswith("2 steps · 5s")

    # A state the gateway grows later still renders, with a neutral mark.
    assert run_row_label(RunRow("6", "w", "brand-new-state", None, None, 1.0)).startswith("◦ ")
    assert set(RUN_BADGES) == {"running", "waiting", "completed", "failed", "cancelled"}


def test_durations_are_coarse_on_purpose() -> None:
    from abstractgateway.tray.app import fmt_duration

    assert fmt_duration(None) == "" and fmt_duration(-1) == ""
    assert fmt_duration(0) == "0s" and fmt_duration(59.9) == "59s"
    assert fmt_duration(60) == "1m 00s" and fmt_duration(133) == "2m 13s"
    assert fmt_duration(3600) == "1h 00m" and fmt_duration(7_845) == "2h 10m"


def test_the_tally_leads_with_what_is_happening_now() -> None:
    from abstractgateway.tray.app import run_tally
    from abstractgateway.tray.sampler import RunRow

    assert run_tally([]) == "No runs in the last 24 hours"

    rows = [
        RunRow("1", "w", "completed", 1, 1.0, 1.0),
        RunRow("2", "w", "running", 1, 1.0, 1.0),
        RunRow("3", "w", "failed", 1, 1.0, 1.0),
        RunRow("4", "w", "completed", 1, 1.0, 1.0),
    ]
    # Running first: it is the only line that is about right now.
    assert run_tally(rows) == "Last 24 hours — 1 running · 2 done · 1 failed"


# ------------------------------------------------- host-wide, not per-tenant


def test_the_run_list_is_host_wide_because_every_other_line_on_the_menu_is(tmp_path) -> None:
    """THE REPORTED DEFECT, pinned.

    The tray asked `GET /runs`, which answers for the CALLING PRINCIPAL's data
    plane. The operator was mid-conversation with an assistant whose runs live
    on the gateway's default plane while the tray's token sat on another, so
    the menu said "No runs in the last 24 hours" about a busy machine. Memory,
    GPU and loaded models on that same menu are host-wide; the run list has to
    be, or it is not describing the same computer.
    """
    from abstractgateway.tray.client import GatewayClient

    asked: List[str] = []

    class _Client(GatewayClient):
        def _request(self, method, path, **kw):  # type: ignore[override]
            asked.append(path)
            from abstractgateway.tray.client import Result

            return Result(True, 200, {"items": []})

    _Client("http://127.0.0.1:1", "t").recent_runs(limit=5)

    assert asked and asked[0].startswith("/host/runs"), asked
    assert not asked[0].startswith("/runs"), "per-principal /runs is the wrong plane for a host view"


def test_a_catalog_workflow_is_named_not_base64_and_machinery_is_hidden() -> None:
    """What the operator is talking to must READ like what they call it.

    A catalog-published workflow runs under `__catalog__v2__…<base64>@0.0.3`.
    Two traps live in that one string: it is the right key and an unreadable
    label, and it starts with `__` — the prefix `/runs` uses to hide the
    gateway's own bookkeeping runs. Hiding it as "internal" would drop exactly
    the work the operator came to see.
    """
    from abstractgateway.admin_runtimes import is_internal_workflow_id, workflow_display_label

    catalog = "__catalog__v2__tenant_catalog__ZGVmYXVsdA__YWJzdHJhY3Rhc3Npc3RhbnQtb3JjaGVzdHJhdG9y@0.0.3:c53b1579"
    assert workflow_display_label(catalog) == "abstractassistant-orchestrator:c53b1579"
    assert is_internal_workflow_id(catalog) is False

    # Real machinery still goes.
    assert is_internal_workflow_id("__session_memory__") is True
    assert is_internal_workflow_id("__anything_else__") is True
    assert is_internal_workflow_id("basic-agent@0.0.4:81795ea9") is False

    # ONE definition, in the module that owns the id scheme — and `/runs` uses
    # it too. The bare `__` prefix was the test until 2026-09-06, which made
    # every catalog-published workflow invisible in the CONSOLE's Runs list as
    # well: an operator mid-conversation with an assistant was shown a machine
    # that had run nothing for a week.
    from abstractgateway.routes.gateway import _is_internal_workflow_id
    from abstractgateway.workflow_catalog import is_internal_workflow_id as canonical

    for probe in (catalog, "__session_memory__", "basic-agent@0.0.4:x", "", None):
        assert _is_internal_workflow_id(probe) is canonical(probe) is is_internal_workflow_id(probe)

    # A plain id keeps its shape, minus the version.
    assert workflow_display_label("basic-agent@0.0.4:81795ea9") == "basic-agent:81795ea9"
    assert workflow_display_label("react-coding@0.1.1:react-coder") == "react-coding:react-coder"
    assert workflow_display_label("") == "" and workflow_display_label(None) == ""


def test_a_generated_flow_hash_is_not_shown_but_a_named_flow_is() -> None:
    from abstractgateway.tray.app import _workflow_menu_name

    # `c53b1579` names nothing to a reader and costs the bundle its width.
    assert _workflow_menu_name("abstractassistant-orchestrator:c53b1579") == "abstractassistant-orchestrator"
    # ...but a flow with a NAME is the half that says what ran.
    assert _workflow_menu_name("react-coding:react-coder") == "react-coding:react-coder"
    # The trim keeps the HEAD: a workflow's identity is the front of its name.
    long_name = _workflow_menu_name("a-really-quite-long-bundle-name-indeed:some-flow")
    assert long_name.startswith("a-really-quite-long-bundle") and long_name.endswith("…")


def test_planes_that_cannot_be_read_cheaply_are_named_not_implied(tmp_path) -> None:
    """An entity plane is reached through the registry, which opens homes and
    wires embedders — work that must never ride a 20-second poll. The payload
    says which planes it skipped instead of implying it saw everything."""
    from abstractgateway.admin_runtimes import recent_runs_host_wide

    (tmp_path / "entities" / "castor").mkdir(parents=True)
    (tmp_path / "entities" / "mira").mkdir(parents=True)

    class _Store:
        def list_runs(self, limit=50):
            return []

    out = recent_runs_host_wide(data_dir=tmp_path, limit=5, default_run_store=_Store())

    assert out["items"] == []
    assert out["skipped_entity_planes"] == ["castor", "mira"]
    assert "default" in out["planes"]


# ----------------------------------------------------------- the menu itself


def _snap(**over: Any):
    from tests.test_gateway_tray_helper import _snap as base  # reuse the one fixture shape

    return base(**over)


def test_a_changed_run_list_rebuilds_the_menu_but_a_ticking_duration_does_not() -> None:
    """`update_menu()` closes the menu under the cursor on Windows/Linux.

    So the signature must carry what the Workflows submenu READS (which runs,
    their state, their step count) and nothing that changes every second.
    """
    from abstractgateway.tray.app import menu_signature
    from abstractgateway.tray.sampler import RunRow

    a = RunRow("1", "w", "running", 4, 10.0, 1.0)
    ticked = RunRow("1", "w", "running", 4, 11.0, 1.0)  # one second later
    stepped = RunRow("1", "w", "running", 5, 11.0, 1.0)
    finished = RunRow("1", "w", "completed", 5, 11.0, 1.0)

    def sig(runs):
        return menu_signature(_snap(runs=runs), update_phase="idle", pending=None, tk_available=False)

    assert sig((a,)) == sig((ticked,)), "a duration ticking must not rebuild the menu"
    assert sig((a,)) != sig((stepped,)), "a new step is worth a rebuild"
    assert sig((stepped,)) != sig((finished,)), "a finished run is worth a rebuild"
    assert sig((a,)) != sig(()), "runs appearing or vanishing is worth a rebuild"


def test_the_menu_has_one_console_door_a_workflows_section_and_unannotated_help(tmp_path) -> None:
    """The three parts of the ruling, read off the menu as RENDERED.

    pystray is not imported here (no display in CI): a stub records what the
    real render path (`TrayApp._menu_items` → pure model → `_render_nodes`)
    hands it, submenus included.
    """
    import sys
    import types

    from abstractgateway.tray import app as tray_app
    from abstractgateway.tray.sampler import RunRow

    captured: List[str] = []

    class _MenuItem:
        def __init__(self, text, action=None, **kw):
            captured.append(str(text))
            self.text, self.action, self.kw = text, action, kw

    class _Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    stub = types.ModuleType("pystray")
    stub.MenuItem = _MenuItem  # type: ignore[attr-defined]
    stub.Menu = _Menu  # type: ignore[attr-defined]

    app = tray_app.TrayApp({"base_url": "http://127.0.0.1:8080", "token": "", "data_dir": str(tmp_path)})
    app._snap = _snap(
        runs=(
            RunRow("1", "coding-agent:coder", "running", 4, 133.0, 1.0),
            RunRow("2", "deep-research:main", "completed", 31, 724.0, 1.0),
        )
    )
    app._tk = False

    saved = sys.modules.get("pystray")
    sys.modules["pystray"] = stub
    try:
        list(app._menu_items())
    finally:
        if saved is None:
            sys.modules.pop("pystray", None)
        else:
            sys.modules["pystray"] = saved

    text = "\n".join(captured)

    # ONE door to the console.
    assert "Open Console" in captured
    assert not any("Show Activity in Console" in t for t in captured)

    # The Workflows section, with the runs and the control under it.
    assert "Workflows" in captured
    assert "Last 24 hours — 1 running · 1 done" in text
    assert "🟢 coding-agent:coder · 4 steps" in text
    assert "✅ deep-research:main · 31 steps · 12m 04s" in text
    assert "Open Runs in Console" in captured
    assert "Pause Workflows" in captured

    # Help names what it opens and nothing else.
    assert "Documentation" in captured and "Report a Problem…" in captured
    assert "Developer API Reference" in captured
    assert "needs internet" not in text and "on this computer" not in text

    # And the icon still has no way to remove itself (the 2026-09-06 ruling).
    assert "Hide" not in text
