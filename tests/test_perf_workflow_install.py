"""The perf job's install is cached and bounded on its own (backlog 1128 item 3).

A 0.20.0 gate run timed out in the perf job's Install step while downloading lancedb (~66 MiB):
setup-python restored a pip cache the install then bypassed (`--no-cache-dir`), and the only bound
was the job's, so a slow download failed as the load test. These pins keep the install on the
cache (keyed on pyproject.toml like the ci jobs) and give it its own timeout."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.basic

PERF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "perf.yml"


def _steps() -> list:
    return yaml.safe_load(PERF.read_text(encoding="utf-8"))["jobs"]["perf"]["steps"]


def test_the_install_uses_the_restored_pip_cache() -> None:
    setup = next(s for s in _steps() if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert setup["with"]["cache"] == "pip" and setup["with"]["cache-dependency-path"] == "pyproject.toml"
    install = next(s for s in _steps() if s.get("name") == "Install")
    assert "--no-cache-dir" not in install["run"]
    assert "pip install" in install["run"]


def test_the_install_has_its_own_bound_inside_the_job() -> None:
    job = yaml.safe_load(PERF.read_text(encoding="utf-8"))["jobs"]["perf"]
    install = next(s for s in job["steps"] if s.get("name") == "Install")
    assert 0 < int(install["timeout-minutes"]) < int(job["timeout-minutes"])
    # The load test keeps at least the 15 minutes it had before the install got its own bound.
    assert int(job["timeout-minutes"]) - int(install["timeout-minutes"]) >= 15
