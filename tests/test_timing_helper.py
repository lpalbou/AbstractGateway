"""tests/timing.py: a wall-clock budget reports by default and asserts only under
ABSTRACTGATEWAY_TIMING_STRICT=1; `patient` stretches a wait bound unless strict."""

from __future__ import annotations

import pytest

from timing import STRICT_ENV, patient, timing_budget

pytestmark = pytest.mark.basic


def test_an_over_budget_reports_and_passes_by_default(monkeypatch, capsys) -> None:
    monkeypatch.delenv(STRICT_ENV, raising=False)
    assert timing_budget(0.8, 0.5, "health p99") is False
    assert "[timing] health p99: 800.0 ms vs limit 500.0 ms (OVER)" in capsys.readouterr().err


def test_strict_asserts_the_budget(monkeypatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    with pytest.raises(AssertionError, match=r"\[timing\] health p99"):
        timing_budget(0.8, 0.5, "health p99")
    assert timing_budget(0.2, 0.5, "health p99") is True
    assert timing_budget(0.5, 0.5, "inclusive", inclusive=True) is True
    with pytest.raises(AssertionError, match="UNDER"):
        timing_budget(3, 6, "health answers during the build", unit="answers", at_least=True)


def test_patient_stretches_a_wait_unless_strict(monkeypatch) -> None:
    monkeypatch.delenv(STRICT_ENV, raising=False)
    assert patient(2.0) == 10.0
    monkeypatch.setenv(STRICT_ENV, "1")
    assert patient(2.0) == 2.0
