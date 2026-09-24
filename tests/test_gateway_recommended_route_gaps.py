"""The recommended starter kit is ADVICE FOR AN EMPTY ROUTE, not a standing debt.

`recommended_model_plan()` answers exactly one question: is the fresh-install
model on this disk? Rendered raw, that answer told an operator who had
deliberately routed `input.text` at their own model that "2 of 3 models
present. Missing: lmstudio qwen/qwen3.5-9b@4bit" -- in error red, above a grid
of three configured, working routes. There was no way to clear it except to
install the model they had chosen against, so it never cleared, and a console
that cries wolf on a healthy host teaches an operator to stop reading it.

The gateway therefore answers the question a console actually needs -- "is any
recommended model missing for a route that has NOTHING else serving it?" -- ONCE,
in `recommended.gaps`, so the web console and the console-TUI cannot drift apart
on it. The raw counts and `would_download` are untouched: `--dry-run` and
`abstractcore config` ask what `--recommended` WOULD fetch, which is a different
question with a different right answer.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.basic


# ---------------------------------------------------------------------------
# What counts as an ANSWERED route
# ---------------------------------------------------------------------------


def test_the_judgement_comes_from_abstractcore_not_a_second_opinion():
    """The seam, pinned: "is this route answered?" is Core's call, not ours.

    It reads four fields Core itself decorates the rows with. A Gateway-side
    re-derivation would drift from `abstractcore models status` and from the
    AbstractCore console the first time one of them gained a coverage lane.
    """

    from abstractgateway import core_config
    from abstractruntime.integrations.abstractcore import config_facade

    assert core_config._mark_recommended_route_gaps.__module__ == core_config.__name__
    assert hasattr(config_facade, "mark_recommended_route_gaps")


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


def _plan():
    """The plan shape AbstractCore returns: the whole starter kit, probed."""

    return {
        "total": 3,
        "installed": 2,
        "absent": 1,
        "unknown": 0,
        "recommended": [
            {"route": "input.text", "provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit", "status": "absent"},
            {"route": "output.voice", "provider": "supertonic", "artifact": "supertonic-3", "status": "installed"},
            {"route": "output.image", "provider": "mlx-gen", "artifact": "flux.2-klein-4b-8bit", "status": "installed"},
        ],
        "would_download": [
            {"route": "input.text", "provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit"},
        ],
    }


def test_a_configured_route_has_no_gap_however_absent_the_recommendation_is():
    """THE REPORTED DEFECT, pinned.

    `input.text` is served by the operator's own remote model. The recommended
    LM Studio build is nowhere on the machine and never will be. That is not a
    gap, and nothing may present it as one.
    """

    from abstractgateway import core_config

    rows = [
        {"key": "input.text", "provider": "airelay", "model": "gpt-5.6-terra"},
        {"key": "output.voice", "provider": "supertonic", "model": "supertonic-3"},
        {"key": "output.image", "provider": "mlx-gen", "model": "flux"},
    ]
    plan = core_config._mark_recommended_route_gaps(_plan(), rows)

    assert plan["gaps"] == []
    assert plan["routes_unanswered"] == 0
    # The raw catalog answer is preserved for the surfaces that ask the raw
    # question -- only the INTERPRETATION is added.
    assert plan["installed"] == 2 and plan["total"] == 3
    assert len(plan["would_download"]) == 1
    assert all(item["route_answered"] for item in plan["recommended"])


def test_an_empty_route_whose_model_is_absent_is_a_gap():
    """The fresh install the starter kit exists for still gets its offer."""

    from abstractgateway import core_config

    rows = [
        {"key": "input.text", "provider": "", "model": "", "source": "not_configured"},
        {"key": "output.voice", "provider": "supertonic", "model": "supertonic-3"},
    ]
    plan = core_config._mark_recommended_route_gaps(_plan(), rows)

    assert plan["routes_unanswered"] == 1
    assert plan["gaps"] == [
        {
            "route": "input.text",
            "provider": "lmstudio",
            "artifact": "qwen/qwen3.5-9b@4bit",
            "route_answered": False,
        }
    ]


def test_a_parent_covered_by_its_task_rows_is_not_a_gap():
    """`output.image` unset with every image task set is a WORKING host.

    Core proves the task rows cover the parent (`capability_route_tasks_cover_broad`);
    asking that host to download the starter image model would be asking it to
    fix nothing.
    """

    from abstractgateway import core_config

    plan = _plan()
    plan["would_download"].append(
        {"route": "output.image", "provider": "mlx-gen", "artifact": "flux.2-klein-4b-8bit"}
    )
    rows = [
        {"key": "input.text", "provider": "airelay", "model": "gpt-5.6-terra"},
        {"key": "output.image", "provider": "", "model": "", "covered_by_tasks": True},
        {"key": "output.image.text_to_image", "provider": "mlx-gen", "model": "flux"},
    ]
    plan = core_config._mark_recommended_route_gaps(plan, rows)

    assert plan["gaps"] == []


def test_gaps_ride_the_availability_payload(monkeypatch):
    """One decision, computed where both consoles read it."""

    from abstractgateway import core_config

    resolved = [
        {"key": "input.text", "provider": "airelay", "model": "gpt-5.6-terra"},
        {"key": "output.voice", "provider": "", "model": ""},
    ]
    monkeypatch.setattr(
        core_config,
        "gateway_capability_defaults_payload",
        lambda **kw: {"ok": True, "routes": resolved, "source": "abstractcore.capability_defaults"},
    )
    monkeypatch.setattr(core_config.config_facade, "annotate_model_availability", lambda rows: list(rows))
    plan = dict(_plan())
    plan["would_download"] = [
        {"route": "input.text", "provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit"},
        {"route": "output.voice", "provider": "supertonic", "artifact": "supertonic-3"},
    ]
    monkeypatch.setattr(core_config.config_facade, "recommended_model_plan", lambda: plan)

    payload = core_config.gateway_model_availability_payload()

    assert [item["route"] for item in payload["recommended"]["gaps"]] == ["output.voice"]
    assert payload["recommended"]["routes_unanswered"] == 1


def test_a_plan_the_probe_could_not_build_is_not_invented(monkeypatch):
    """A failed recommendation probe leaves the banner silent, not alarmed."""

    from abstractgateway import core_config

    monkeypatch.setattr(
        core_config,
        "gateway_capability_defaults_payload",
        lambda **kw: {"ok": True, "routes": [{"key": "input.text", "provider": "p", "model": "m"}]},
    )
    monkeypatch.setattr(core_config.config_facade, "annotate_model_availability", lambda rows: list(rows))

    def boom():
        raise RuntimeError("lms is wedged")

    monkeypatch.setattr(core_config.config_facade, "recommended_model_plan", boom)

    payload = core_config.gateway_model_availability_payload()

    assert payload["recommended"] == {}
    assert any("recommended model probe failed" in err for err in payload["errors"])


# ---------------------------------------------------------------------------
# The console contract
# ---------------------------------------------------------------------------


def test_the_console_banner_speaks_only_about_gaps():
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()

    # It reads the interpreted field, never the raw catalog count.
    assert "plan.gaps" in html
    assert "models present" not in html, "the raw 'N of M models present' count is gone"
    # "Apply recommended" is a standing head action now, so the banner never has
    # to render just to keep it reachable.
    assert 'id="defaults-apply-recommended"' in html
    assert '<span>Apply recommended</span>' in html
    # And "Download missing" fetches the artifacts the banner named, one by one
    # -- not the whole starter kit for routes that are already answered. The
    # check is scoped to the banner's download function: the first-run guide's
    # "Download all" is a DIFFERENT, documented action that posts
    # `{recommended: true}` on purpose (one parent job over the whole set,
    # docs/model-downloads.md), and a page-wide check would forbid it.
    start = html.index("async function downloadRecommended(")
    following = re.search(r"\n\s*(?:async )?function \w+\(", html[start + 1 :])
    banner_download = html[start : start + 1 + following.start()]
    assert "gaps" in banner_download
    assert "recommended: true" not in banner_download.replace("`{recommended: true}`", "")
    assert "recommended: true })" not in banner_download
    assert "Download missing" in html
