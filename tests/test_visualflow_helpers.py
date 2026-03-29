"""
Tests for gateway VisualFlow helper functions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT / "abstractgateway" / "src"))

from abstractgateway.routes import gateway as gw  # noqa: E402


def test_safe_visualflow_id_accepts_safe_values() -> None:
    assert gw._safe_visualflow_id("flow-1_ok") == "flow-1_ok"
    assert gw._safe_visualflow_id("flow.2") == "flow.2"


def test_safe_visualflow_id_rejects_unsafe_values() -> None:
    with pytest.raises(HTTPException):
        gw._safe_visualflow_id("")
    with pytest.raises(HTTPException):
        gw._safe_visualflow_id("..")
    with pytest.raises(HTTPException):
        gw._safe_visualflow_id("../escape")
    with pytest.raises(HTTPException):
        gw._safe_visualflow_id("bad/segment")


def test_semver_parsing_and_bump_patch() -> None:
    assert gw._try_parse_semver("1.2.3") == (1, 2, 3)
    assert gw._try_parse_semver("1.2") == (1, 2, 0)
    assert gw._try_parse_semver("1") == (1, 0, 0)
    assert gw._try_parse_semver("1.a") is None

    assert gw._bump_patch("1.2.3") == "1.2.4"
    assert gw._bump_patch("1") == "1.0.1"
    assert gw._bump_patch("beta") == "beta.1"
    assert gw._bump_patch("") == "0.0.1"


def test_version_ordering_helpers() -> None:
    versions = [{"bundle_version": "0.1.0"}, {"bundle_version": "0.2.0"}, {"bundle_version": "0.1.5"}]
    assert gw._latest_version(versions) == "0.2.0"

    dated = [
        {"bundle_version": "alpha", "created_at": "2024-01-01T00:00:00Z"},
        {"bundle_version": "beta", "created_at": "2024-02-01T00:00:00Z"},
    ]
    assert gw._latest_version(dated) == "beta"
    assert gw._origin_version(dated) == "alpha"
