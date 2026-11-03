"""Backlog 0972: in CI every seam package is installed, so its routes are in the table the route
authorization contract walks. A check whose absent-input case passes is decoration: this test is
what fails when a seam package drops out of the CI install step.

`CI_SEAMS_REQUIRED` (the CI workflow sets it, a test-harness knob) lists the seams that must be
mounted; without it (a developer machine without the package) the test is skipped, saying so.
"""
from __future__ import annotations

import os

import pytest

REQUIRED = [s.strip() for s in os.environ.get("CI_SEAMS_REQUIRED", "").split(",") if s.strip()]


@pytest.mark.skipif(not REQUIRED, reason="CI_SEAMS_REQUIRED is not set (CI sets it): seam packages are optional here")
def test_every_required_seam_is_mounted_in_the_live_route_table() -> None:
    assert set(REQUIRED) <= {"abstractnote"}, f"unknown seam in CI_SEAMS_REQUIRED: {REQUIRED}"
    from abstractgateway.abstractnote_seam import abstractnote_available
    from test_gateway_route_authorization_contract import _live_route_table  # the table the contract walks

    assert abstractnote_available(), "abstractnote is required in CI but does not import"
    note = sorted((m, p) for m, p in _live_route_table() if p.startswith("/api/gateway/note"))
    assert note, "the AbstractNote routes are not in the gateway's route table"
    assert {m for m, _p in note} & {"POST", "PUT", "PATCH", "DELETE"}, (
        "no note write route mounted: the authorization contract would check nothing")
