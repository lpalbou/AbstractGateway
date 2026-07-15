"""Drift pin against THE canonical phase-graph artifact (laurent 13:54:
entity owns spec/entity_phases.json; every seat verifies its lane against
THAT file, never against re-derived prose — the diary_type two-copies class
dies this way).

The consumption contract's executor half: tests IMPORT the artifact directly
from the sibling checkout. Standalone checkouts skip with a visible warning
(observer's precedent), never a false green.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

_ARTIFACT = Path(__file__).resolve().parents[2] / "abstractentity" / "spec" / "entity_phases.json"


def _load_artifact() -> dict:
    if not _ARTIFACT.exists():
        warnings.warn(
            f"#FALLBACK phase artifact not found at {_ARTIFACT} (standalone checkout?) — pin skipped",
            stacklevel=1,
        )
        pytest.skip("sibling abstractentity checkout not present")
    return json.loads(_ARTIFACT.read_text(encoding="utf-8"))


def test_cognition_enum_matches_the_artifact():
    """/cognition's strict phase keys are exactly the artifact's phase set
    (plus null for the artifact's own recorded AWAKE-IDLE open question)."""
    art = _load_artifact()
    artifact_phases = set((art.get("phases") or {}).keys())
    assert artifact_phases == {"visit", "work", "personal", "sleep"}
    # The gateway fold can emit each artifact key or None — asserted against
    # the served vocabulary in test_gateway_entity_cognition (folds); here we
    # pin that the VOCABULARY SOURCE agrees with runtime's canonical tuple.
    from abstractruntime.identity.tool_policy import PHASES

    assert set(PHASES) == artifact_phases


def test_grant_modes_and_initial_phase_match_the_artifact():
    art = _load_artifact()
    personal = (art.get("phases") or {}).get("personal") or {}
    # The artifact's grant modes are runtime's PERSONAL_GRANT_MODES (one
    # vocabulary — my door imports runtime's constants, never respells).
    from abstractruntime.identity.life import PERSONAL_GRANT_MODES

    art_modes = personal.get("grant_modes") or personal.get("activation", {}).get("modes")
    if art_modes is not None:
        assert set(art_modes) == set(PERSONAL_GRANT_MODES)
    # newborn=sleep is the artifact's initial_phase; gateway's create() does
    # not write it yet — the KNOWN parked nonconformance (c1501: awaiting the
    # eligibility-vs-state fix shape). This pin makes the artifact's word
    # visible in MY suite so the flip is driven by the source, not memory.
    assert art.get("initial_phase") == "sleep"
