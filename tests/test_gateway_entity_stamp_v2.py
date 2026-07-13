"""entity-stamp-v2: phase (+participants +reflection_nodes) INSIDE the MAC,
ACCEPT-OLD-AS-VISIT migration (config-object plan N2 + close-reflection
ruling; proof row R1).

Before v2, the stamp signed only (entity_id, channel, session_id, nonce,
run_id) — a validly-stamped run could flip its unsigned `phase` from sleep
to personal and grab the full toolset, and its unsigned `participants`
(engraved as co-presence + valence targets) were forgeable. v2 folds
phase + participants + reflection_nodes into the signed basis with the
version tag INSIDE the MAC, so a v2 stamp cannot be downgraded to v1 by
stripping the phase field.

R1 proof shape (agency c675): phase is signed and resolves through one
resolver; a v1 (phase-less) stamp resolves visit authority EXACTLY (not a
widen); a v2 stamp with a TAMPERED phase fails MAC verification outright.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

from abstractgateway.entity_gate import (  # noqa: E402
    CHANNEL_WORKPLACE,
    finalize_summon_stamp,
    mint_summon_stamp,
    resolved_phase,
    verify_summon_stamp,
)


class _Run:
    """The minimal duck-typed run verify_summon_stamp reads."""

    def __init__(self, run_id: str, session_id: str) -> None:
        self.run_id = run_id
        self.session_id = session_id


def _minted(
    data_dir: Path,
    *,
    phase: str = "visit",
    participants=None,
    reflection_nodes=None,
    session_id: str = "sess-1",
    run_id: str = "run-1",
) -> dict:
    prov = mint_summon_stamp(
        data_dir=data_dir,
        entity_id="entity:castor",
        channel=CHANNEL_WORKPLACE,
        session_id=session_id,
        participants=participants if participants is not None else ["person:laurent", "entity:castor"],
        phase=phase,
        reflection_nodes=reflection_nodes,
    )
    return finalize_summon_stamp(prov, data_dir=data_dir, run_id=run_id)


def test_v2_stamp_verifies_and_resolves_its_phase(tmp_path: Path) -> None:
    for phase, expected in [("visit", "visit"), ("work", "work"), ("personal", "personal"), ("sleep", "sleep")]:
        stamp = _minted(tmp_path, phase=phase)
        ok, err = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-1", "sess-1"))
        assert ok, f"phase={phase}: {err}"
        assert resolved_phase(stamp) == expected


def test_phase_is_canonicalized_at_mint(tmp_path: Path) -> None:
    """A legacy 'resident'/'own_time' spelling normalizes to the ruled
    'personal' at mint (laurent c786: own_time→personal), so the MAC signs
    the canonical phase and the resolver never sees the alias."""
    for legacy in ("resident", "own_time"):
        stamp = _minted(tmp_path, phase=legacy)
        assert stamp["phase"] == "personal", legacy
        ok, _ = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-1", "sess-1"))
        assert ok
        assert resolved_phase(stamp) == "personal"


def test_unknown_phase_refused_at_mint(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        mint_summon_stamp(
            data_dir=tmp_path,
            entity_id="entity:castor",
            channel=CHANNEL_WORKPLACE,
            session_id="s",
            participants=[],
            phase="superuser",
        )


def test_stamp_signed_under_a_previous_canon_survives_a_rename(tmp_path: Path) -> None:
    """Runtime adversary F7 (c814): the MAC verifies the SIGNED BYTES
    VERBATIM — canonicalization is mint-time (into the stored stamp) and
    resolution-time (canonical_phase), NEVER inside the verify recompute.

    A parked durable run whose stamp was signed when the canon spelled the
    phase 'own_time' must RESUME after the own_time→personal rename: verify
    passes on the persisted bytes, and resolved_phase maps the legacy
    spelling forward. If verify re-canonicalized, every such run would
    self-DoS at resume on a pure vocabulary move — zero security gain (any
    byte tamper still fails compare_digest).

    Construction bypasses mint (which canonicalizes) by finalizing a
    provisional whose stored phase is the LEGACY spelling — exactly the
    shape of a stamp persisted before the rename."""
    prov = mint_summon_stamp(
        data_dir=tmp_path,
        entity_id="entity:castor",
        channel=CHANNEL_WORKPLACE,
        session_id="sess-old",
        participants=["person:laurent", "entity:castor"],
        phase="visit",  # minted normally...
    )
    prov["phase"] = "own_time"  # ...then persisted under the PREVIOUS canon
    stamp = finalize_summon_stamp(prov, data_dir=tmp_path, run_id="run-old")

    ok, err = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-old", "sess-old"))
    assert ok, f"legacy-canon stamp must verify on its signed bytes: {err}"
    assert resolved_phase(stamp) == "personal", "resolution maps the legacy spelling forward"

    # Tampering the legacy phase still fails — verbatim-bytes verification
    # is not a weakening.
    forged = dict(stamp)
    forged["phase"] = "personal"
    ok, err = verify_summon_stamp(forged, data_dir=tmp_path, run=_Run("run-old", "sess-old"))
    assert not ok and "signature is invalid" in err


def test_tampered_phase_fails_mac(tmp_path: Path) -> None:
    """The N2 attack: flip a signed sleep stamp to personal. The MAC was
    computed over phase='sleep'; mutating the field makes the recompute
    diverge and verification fails outright — no authority flip."""
    stamp = _minted(tmp_path, phase="sleep")
    stamp["phase"] = "personal"  # the forgery
    ok, err = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-1", "sess-1"))
    assert not ok
    assert "signature is invalid" in err


def test_tampered_participants_fails_mac(tmp_path: Path) -> None:
    """participants are engraved as co-presence + valence targets — v2 signs
    them, so injecting a witness after mint fails verification."""
    stamp = _minted(tmp_path, participants=["person:laurent", "entity:castor"])
    stamp["participants"] = stamp["participants"] + ["person:mallory"]
    ok, err = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-1", "sess-1"))
    assert not ok
    assert "signature is invalid" in err


def test_tampered_reflection_nodes_fails_mac(tmp_path: Path) -> None:
    """The close-reflection lever: adding a node to the signed segment set
    (which would widen that node's effects to entity-reflection) fails MAC."""
    stamp = _minted(tmp_path, reflection_nodes=["REFLECT", "APPLY"])
    stamp["reflection_nodes"] = ["REFLECT", "APPLY", "REASON"]  # the escalation attempt
    ok, err = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-1", "sess-1"))
    assert not ok
    assert "signature is invalid" in err


def test_downgrade_by_stripping_phase_fails(tmp_path: Path) -> None:
    """A v2 stamp with its phase field DELETED must not silently verify as a
    v1 stamp — the version tag is inside the MAC, so the v1 recompute
    diverges from the v2-basis signature."""
    stamp = _minted(tmp_path, phase="sleep")
    del stamp["phase"]  # attempt to become "just a v1 stamp resolving visit"
    ok, err = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-1", "sess-1"))
    assert not ok
    assert "signature is invalid" in err


def test_legacy_v1_stamp_resolves_visit_authority_exactly(tmp_path: Path) -> None:
    """ACCEPT-OLD-AS-VISIT: a genuinely phase-less v1 stamp (minted before
    the migration; simulated by signing the v1 basis directly) STILL
    verifies and resolves phase='visit' — identical authority to
    pre-migration, so parked/in-flight runs are never bricked."""
    from abstractgateway.entity_gate import _sign_v1, _stamp_secret

    secret = _stamp_secret(tmp_path)
    v1 = {
        "entity_id": "entity:castor",
        "channel": CHANNEL_WORKPLACE,
        "session_id": "sess-1",
        "participants": ["person:laurent", "entity:castor"],  # present but UNSIGNED in v1
        "nonce": "abc123",
        "run_id": "run-1",
    }
    v1["sig"] = _sign_v1(
        secret,
        entity_id=v1["entity_id"],
        channel=v1["channel"],
        session_id=v1["session_id"],
        nonce=v1["nonce"],
        run_id=v1["run_id"],
    )
    ok, err = verify_summon_stamp(v1, data_dir=tmp_path, run=_Run("run-1", "sess-1"))
    assert ok, err
    assert resolved_phase(v1) == "visit"  # NOT a widen; visit was the only pre-migration authority


def test_run_binding_and_session_binding_still_enforced(tmp_path: Path) -> None:
    """v2 must not regress the v1 guards: a stamp bound to run-1/sess-1 must
    fail against a different run id or a session-mismatched run."""
    stamp = _minted(tmp_path, phase="work", run_id="run-1", session_id="sess-1")
    ok, _ = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-OTHER", "sess-1"))
    assert not ok
    ok, _ = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run("run-1", "sess-OTHER"))
    assert not ok


def test_three_doors_one_entity_distinguishable_phases(tmp_path: Path) -> None:
    """R1 core: the SAME entity stamped for three different phases yields
    three verifying stamps whose resolved phase differs — the door, not the
    payload, decides which authority column a session runs in."""
    seen = {}
    for phase in ("visit", "work", "sleep"):
        stamp = _minted(tmp_path, phase=phase, session_id=f"sess-{phase}", run_id=f"run-{phase}")
        ok, err = verify_summon_stamp(stamp, data_dir=tmp_path, run=_Run(f"run-{phase}", f"sess-{phase}"))
        assert ok, err
        seen[phase] = resolved_phase(stamp)
    assert seen == {"visit": "visit", "work": "work", "sleep": "sleep"}
