"""Card 014: host-marker lane capacity + flood detection.

The 2026-07-14 marker-flood incident (995 diary_read markers on one journal
base) proved the 999-per-base fan-out wedges a life's audit stream: once the
budget is spent, EVERY later host moment at that base is lost. These pins
cover the repair:

- capacity: new writes mint 1/10000 ticks (9999 slots per base) — a base
  absorbs >999 markers without wedging;
- coexistence: legacy 1/1000 markers stay engraved untouched; new markers
  slot strictly after them, in order, in one file;
- detection: a same-(kind, reason) burst above the threshold logs a LOUD
  warning naming the signature — the append always proceeds (detection
  only; coalescing is maintainer-gated);
- exhaustion: the true ceiling still refuses loudly rather than colliding;
- read bounds: `marker_window_end` is exact for any granularity (the old
  hand-tuned epsilons silently excluded high-tick markers).
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractmemory.replay")

from abstractgateway.entity_replay import (  # noqa: E402
    MARKER_FLOOD_THRESHOLD,
    MARKER_TICKS_PER_BASE,
    marker_window_end,
    read_host_markers,
    record_host_marker,
)

_ENTITY = "entity:castor"


def _record(entities_dir: Path, *, kind: str = "diary_read", journal_seq: int = 42, details=None):
    return record_host_marker(
        entities_dir=entities_dir,
        slug="castor",
        entity_id=_ENTITY,
        kind=kind,
        journal_seq=journal_seq,
        details=details,
    )


def _engrave(entities_dir: Path, rows: list) -> Path:
    """Hand-write marker rows the way a historical file carries them —
    engraved data, not API output."""
    path = entities_dir / ".host_stream" / "castor.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _legacy_row(seq: float, kind: str = "summon", observed_at: str = "2026-07-14T04:11:00+00:00") -> dict:
    return {
        "stream": "abstractmemory.replay",
        "stream_version": 1,
        "seq": seq,
        "family": "host",
        "observed_at": observed_at,
        "scope": "",
        "owner_id": _ENTITY,
        "trace_id": None,
        "turn_id": None,
        "run_id": None,
        "payload": {"kind": kind, "session_id": None},
    }


# ------------------------------------------------------------------ capacity


def test_dense_base_absorbs_beyond_the_old_999_cap(tmp_path: Path):
    """The incident wedge: marker #1000 on one base used to raise. A base
    now absorbs >999 markers, strictly ascending, collision-free, all
    strictly inside (base, base+1)."""
    n = 1005
    seqs = [_record(tmp_path, journal_seq=42, details={"reason": "dense-base"})["seq"] for _ in range(n)]

    assert len(seqs) == n
    assert seqs == sorted(seqs), "append order must be seq order at one base"
    assert len(set(seqs)) == n, "no fractional-seq collisions"
    assert all(42.0 < s < 43.0 for s in seqs), "markers never touch the journal seqs"

    got = read_host_markers(tmp_path, "castor")
    assert len(got) == n, "detection never skips an append"


def test_legacy_thousandths_and_new_ticks_coexist_in_order(tmp_path: Path):
    """Old floats stay engraved; new writes slot strictly after them at the
    same base and stay below base+1 — one file, one total order."""
    legacy_seqs = [13 + i / 1000.0 for i in (1, 2, 3)]
    _engrave(tmp_path, [_legacy_row(s) for s in legacy_seqs])

    new1 = _record(tmp_path, kind="summon", journal_seq=13)["seq"]
    new2 = _record(tmp_path, kind="session_closed", journal_seq=13)["seq"]
    other_base = _record(tmp_path, kind="wake", journal_seq=14)["seq"]

    assert new1 > max(legacy_seqs), "new granularity sorts after every engraved legacy marker"
    assert new2 > new1
    assert new1 < 14.0 and new2 < 14.0
    assert 14.0 < other_base < 15.0

    got = [m["seq"] for m in read_host_markers(tmp_path, "castor")]
    assert got == sorted(got) and len(set(got)) == len(got)
    assert got == legacy_seqs + [new1, new2, other_base]


def test_slot_ladder_survives_float_subtraction_artifacts(tmp_path: Path):
    """Engraved floats like 13.001 lose exactness under subtraction
    (13.001 - 13 != 0.001); the ladder compares in final float space so the
    next marker is STRICTLY above the engraved one, never equal."""
    _engrave(tmp_path, [_legacy_row(13.001)])
    nxt = _record(tmp_path, kind="summon", journal_seq=13)["seq"]
    assert nxt > 13.001
    assert math.floor(nxt) == 13


# ----------------------------------------------------------------- detection


def test_flood_detection_warns_loudly_and_never_blocks(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Same (kind, reason) above the threshold inside the window: ONE loud
    warning naming the signature; every append still lands."""
    with caplog.at_level(logging.WARNING, logger="abstractgateway.entity_replay"):
        for i in range(MARKER_FLOOD_THRESHOLD):
            _record(tmp_path, journal_seq=7, details={"entry_id": f"e{i}", "reason": "operator review"})

    floods = [r for r in caplog.records if "host-marker flood" in r.getMessage()]
    assert len(floods) == 1, "warn at the crossing, not on every append"
    msg = floods[0].getMessage()
    assert "diary_read" in msg and "operator review" in msg and "castor" in msg

    assert len(read_host_markers(tmp_path, "castor")) == MARKER_FLOOD_THRESHOLD


def test_distinct_signatures_below_threshold_stay_quiet(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """The signature is (kind, reason): a mixed bag under the per-signature
    threshold must not warn."""
    with caplog.at_level(logging.WARNING, logger="abstractgateway.entity_replay"):
        for i in range(MARKER_FLOOD_THRESHOLD - 1):
            _record(tmp_path, journal_seq=7, details={"reason": "operator review"})
        _record(tmp_path, journal_seq=7, details={"reason": "doctoring pass"})
        _record(tmp_path, kind="wake", journal_seq=7, details={"reason": "operator review"})

    assert not [r for r in caplog.records if "host-marker flood" in r.getMessage()]


def test_stale_markers_outside_the_window_do_not_count(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Detection is a RATE check: an old flood (historical file content)
    must not make the next lone marker warn."""
    stale = [
        {**_legacy_row(5 + (i + 1) / 1000.0, kind="diary_read"), "payload": {"kind": "diary_read", "session_id": None, "reason": "operator review"}}
        for i in range(MARKER_FLOOD_THRESHOLD + 5)
    ]
    _engrave(tmp_path, stale)

    with caplog.at_level(logging.WARNING, logger="abstractgateway.entity_replay"):
        _record(tmp_path, journal_seq=6, details={"reason": "operator review"})

    assert not [r for r in caplog.records if "host-marker flood" in r.getMessage()]


# ---------------------------------------------------------------- exhaustion


def test_true_exhaustion_still_refuses_loudly(tmp_path: Path):
    """A marker at the LAST tick of a base: the next append at that base
    raises (never collides, never spills into base+1); other bases are
    unaffected."""
    last_tick_seq = 5 + (MARKER_TICKS_PER_BASE - 1) / MARKER_TICKS_PER_BASE
    _engrave(tmp_path, [_legacy_row(last_tick_seq)])

    with pytest.raises(RuntimeError, match="fan-out exhausted"):
        _record(tmp_path, kind="summon", journal_seq=5)

    ok = _record(tmp_path, kind="summon", journal_seq=6)["seq"]
    assert 6.0 < ok < 7.0


# --------------------------------------------------------------- read bounds


def test_marker_window_end_is_exact_for_any_granularity(tmp_path: Path):
    """`until_seq=marker_window_end(base)` includes every marker anchored
    at bases <= base — including high ticks the old `+ 0.9995` epsilon
    silently excluded — and excludes base+1's markers exactly."""
    high_tick = 13 + (MARKER_TICKS_PER_BASE - 1) / MARKER_TICKS_PER_BASE  # 13.9999
    next_base_first = 14 + 1 / MARKER_TICKS_PER_BASE  # 14.0001
    _engrave(tmp_path, [_legacy_row(high_tick), _legacy_row(next_base_first)])

    got = [m["seq"] for m in read_host_markers(tmp_path, "castor", until_seq=marker_window_end(13))]
    assert got == [high_tick]

    assert high_tick > 13 + 0.9995, "the old epsilon would have dropped this marker"
    assert marker_window_end(13) < 14.0
    assert marker_window_end(13) >= high_tick
