"""N4 sleep-deposit gate + close-reflection segment authority — the deposit
gate's phase dimension (config-object plan; proof row R5 + the R1
close-reflection spoof pins).

These exercise the pure gate functions directly (the router threads the
verified phase + host-written segment membership into them). Two rulings:

- N4 (memory c673/c678): at verified phase==sleep the gate refuses
  MEMORY_FORM, MEMORY_ADJUST, MEMORY_APPRAISE, and the MEMORY_ACCESS commit,
  channel-independent, segment-independent. Pure recall reads stay open.
- close-reflection (agency c705, ruled option (a)): a WORKPLACE visit run
  executing a door-signed reflection node may run the NARROW act set
  (interest→self, summary-with-summarizes-edges, routine APPRAISE) as
  entity-reflection; anything else in the segment refuses; the sleep refusal
  PRECEDES segment widening (memory c714 gate order).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.basic

from abstractgateway.entity_gate import (  # noqa: E402
    CHANNEL_ENTITY_REFLECTION,
    CHANNEL_WORKPLACE,
    _gate_access,
    _gate_adjust,
    _gate_appraise,
    _gate_diary_write,
    _gate_form,
    in_reflection_segment,
)


def _stamp(*, channel: str = CHANNEL_WORKPLACE, reflection_nodes=None) -> dict:
    return {
        "entity_id": "entity:castor",
        "channel": channel,
        "session_id": "sess-1",
        "participants": ["person:laurent", "entity:castor"],
        "reflection_nodes": list(reflection_nodes or []),
    }


class _Run:
    def __init__(self, current_node: str) -> None:
        self.current_node = current_node
        self.session_id = "sess-1"
        self.run_id = "run-1"


# ---------------------------------------------------------------- N4 sleep gate

def test_sleep_refuses_form_adjust_appraise_access_diary():
    stamp = _stamp()
    form_err = _gate_form({"scope": "life", "records": [{"kind": "episode"}]}, stamp=stamp, phase="sleep")
    assert form_err and "deposits nothing" in form_err
    adjust_err = _gate_adjust({"scope": "life", "op": "salience"}, stamp=stamp, phase="sleep")
    assert adjust_err and "deposits nothing" in adjust_err
    appraise_err = _gate_appraise({"scope": "self"}, stamp=stamp, phase="sleep")
    assert appraise_err and "deposits nothing" in appraise_err
    access_err = _gate_access({}, stamp=stamp, phase="sleep")
    assert access_err and "deposits nothing" in access_err
    # DIARY_WRITE is a graph-writing projection — sleep must refuse it too
    # (defense-in-depth; adversary find that the doc claimed cover it did not).
    diary_err = _gate_diary_write({"entry": "x"}, stamp=stamp, phase="sleep")
    assert diary_err and "deposits nothing" in diary_err


def test_diary_write_open_outside_sleep():
    """The diary is the entity's elected act while awake — no phase gate on
    visit/work/personal (the author binds at construction)."""
    for phase in ("visit", "work", "personal"):
        assert _gate_diary_write({"entry": "x"}, stamp=_stamp(), phase=phase) is None


def test_sleep_refusal_precedes_segment_widening():
    """Even if a sleep run somehow presented a reflection segment, the sleep
    refusal fires FIRST (memory c714 gate order) — a future phase can't be
    argued around via segment membership."""
    stamp = _stamp(reflection_nodes=["REFLECT", "APPLY"])
    err = _gate_form(
        {"scope": "self", "records": [{"kind": "interest"}]},
        stamp=stamp,
        phase="sleep",
        segment=True,
    )
    assert err and "deposits nothing" in err


def test_non_sleep_phases_allow_ordinary_workplace_form():
    """visit/work/personal deposit normally (a life-scope episode is a
    workplace act); only sleep bars deposits."""
    for phase in ("visit", "work", "personal"):
        err = _gate_form({"scope": "life", "records": [{"kind": "episode"}]}, stamp=_stamp(), phase=phase)
        assert err is None, f"phase={phase}: {err}"


def test_access_open_outside_sleep():
    for phase in ("visit", "work", "personal"):
        assert _gate_access({}, stamp=_stamp(), phase=phase) is None


# ------------------------------------------------ close-reflection segment (R1 spoof pins)

def test_workplace_interest_into_self_refused_outside_segment():
    """The collision agency caught: a workplace visit forming interest→self
    OUTSIDE the reflection segment is refused (identity is not a workplace
    act)."""
    err = _gate_form(
        {"scope": "self", "records": [{"kind": "interest", "digest": "x"}]},
        stamp=_stamp(),
        phase="visit",
        segment=False,
    )
    assert err and "not a workplace act" in err


def test_interest_into_self_ALLOWED_in_segment_as_entity_reflection():
    """Ruling (a): inside the signed reflection segment the interest→self
    FORM resolves as the entity's own reflection — no refusal, and the door
    stamps provenance.actor=entity-reflection (payload claims decorative)."""
    payload = {"scope": "self", "records": [{"kind": "interest", "digest": "x", "provenance": {"actor": "spoofed"}}]}
    err = _gate_form(payload, stamp=_stamp(reflection_nodes=["REFLECT", "APPLY"]), phase="visit", segment=True)
    assert err is None
    assert payload["records"][0]["provenance"]["actor"] == CHANNEL_ENTITY_REFLECTION


def test_summary_with_summarizes_edges_allowed_in_segment():
    payload = {
        "scope": "life",
        "records": [{"kind": "summary", "digest": "s", "edges": [["summarizes", "ex:1"]]}],
    }
    err = _gate_form(payload, stamp=_stamp(reflection_nodes=["REFLECT", "APPLY"]), phase="visit", segment=True)
    assert err is None


def test_summary_without_edges_refused_in_segment():
    payload = {"scope": "life", "records": [{"kind": "summary", "digest": "s"}]}
    err = _gate_form(payload, stamp=_stamp(reflection_nodes=["REFLECT", "APPLY"]), phase="visit", segment=True)
    assert err and "summarizes" in err


def test_non_listed_identity_kind_refused_in_segment():
    """Spoof pin 3 (memory c714): an in-segment FORM of a non-listed kind
    (e.g. value into self) refuses — identity kinds beyond interest stay
    untouchable through every channel a visit carries."""
    payload = {"scope": "self", "records": [{"kind": "value", "digest": "I am brave"}]}
    err = _gate_form(payload, stamp=_stamp(reflection_nodes=["REFLECT", "APPLY"]), phase="visit", segment=True)
    assert err and "not a reflection act" in err


def test_appraise_in_segment_derives_entity_reflection_actor():
    payload = {"scope": "self"}
    err = _gate_appraise(payload, stamp=_stamp(reflection_nodes=["REFLECT", "APPLY"]), phase="visit", segment=True)
    assert err is None
    assert payload["actor"] == CHANNEL_ENTITY_REFLECTION


def test_appraise_outside_segment_derives_workplace_actor():
    payload = {"scope": "self"}
    err = _gate_appraise(payload, stamp=_stamp(), phase="visit", segment=False)
    assert err is None
    assert payload["actor"] == "workplace:sess-1"


# ----------------------------------------------- segment membership (host-written only)

def test_in_reflection_segment_requires_workplace_and_signed_node():
    stamp = _stamp(reflection_nodes=["REFLECT", "APPLY"])
    assert in_reflection_segment(stamp, _Run("APPLY")) is True
    assert in_reflection_segment(stamp, _Run("REASON")) is False  # not a signed node
    assert in_reflection_segment(stamp, _Run("")) is False
    # entity-reflection / operator channels are already privileged — no segment
    assert in_reflection_segment(_stamp(channel=CHANNEL_ENTITY_REFLECTION, reflection_nodes=["APPLY"]), _Run("APPLY")) is False
    # a stamp with no signed reflection nodes never widens
    assert in_reflection_segment(_stamp(), _Run("APPLY")) is False
