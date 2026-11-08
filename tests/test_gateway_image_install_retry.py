"""The GHCR image build retries the pinned PyPI install while the index catches up (root 1133).

The v0.20.2 release run 42339708494 failed inside the image build with "No matching distribution
found for abstractgateway==0.20.2", minutes after the publish and although the runner-side wait had
seen the version. Both Dockerfiles install through `install-gateway.sh`, which retries a pinned PyPI
install with a bounded backoff. These tests run the real script under `sh` with a stand-in `python`
that fails a given number of times and records every call."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.basic, pytest.mark.skipif(os.name != "posix", reason="the image script runs under /bin/sh")]

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker" / "abstractgateway-server"
SCRIPT = DOCKER / "install-gateway.sh"


def _run(tmp_path: Path, *, failures: int, delays: str = "0 0 0", **env_extra: str) -> tuple[int, list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.txt"
    count = tmp_path / "count.txt"
    count.write_text("0")
    # Stand-in python: records its argv, fails the first `failures` calls like pip's resolver does.
    fake = bin_dir / "python"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        f'n=$(cat "{count}"); n=$((n + 1)); echo "$n" > "{count}"\n'
        f'if [ "$n" -le {failures} ]; then echo "ERROR: No matching distribution found" >&2; exit 1; fi\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin", "ABSTRACTGATEWAY_PIP_RETRY_DELAYS": delays}
    env.update(env_extra)
    proc = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    lines = calls.read_text().splitlines() if calls.exists() else []
    return proc.returncode, lines, proc.stderr


def test_a_pinned_install_is_retried_until_the_index_has_it(tmp_path: Path) -> None:
    code, calls, err = _run(tmp_path, failures=2, ABSTRACTGATEWAY_VERSION="0.21.0")
    assert code == 0, err
    assert calls == ["-m pip install abstractgateway==0.21.0"] * 3
    assert "attempt 1" in err and "attempt 2" in err


def test_the_retries_are_bounded_and_the_build_fails(tmp_path: Path) -> None:
    code, calls, err = _run(tmp_path, failures=99, delays="0 0 0", ABSTRACTGATEWAY_VERSION="0.21.0")
    assert code == 1
    assert len(calls) == 4  # the first try plus one retry per delay
    assert "could not install abstractgateway==0.21.0 after 4 attempts" in err


def test_extras_are_kept_in_the_pinned_spec(tmp_path: Path) -> None:
    code, calls, _ = _run(tmp_path, failures=1, ABSTRACTGATEWAY_VERSION="0.21.0", ABSTRACTGATEWAY_EXTRAS="gpu")
    assert code == 0 and calls == ["-m pip install abstractgateway[gpu]==0.21.0"] * 2


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"ABSTRACTGATEWAY_INSTALL_MODE": "local"}, "-m pip install /tmp/abstractgateway-src"),
        ({"ABSTRACTGATEWAY_INSTALL_MODE": "local", "ABSTRACTGATEWAY_EXTRAS": "gpu"}, "-m pip install /tmp/abstractgateway-src[gpu]"),
        ({}, "-m pip install abstractgateway"),
    ],
)
def test_local_and_unpinned_installs_run_once(tmp_path: Path, env: dict, expected: str) -> None:
    code, calls, _ = _run(tmp_path, failures=1, **env)
    assert code == 1 and calls == [expected]  # not retried: nothing to wait for


def test_an_unknown_mode_fails_loudly(tmp_path: Path) -> None:
    code, calls, err = _run(tmp_path, failures=0, ABSTRACTGATEWAY_INSTALL_MODE="pypy")
    assert code == 2 and calls == [] and "unknown ABSTRACTGATEWAY_INSTALL_MODE" in err


def test_the_default_backoff_is_bounded() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    default = text.split('delays="${ABSTRACTGATEWAY_PIP_RETRY_DELAYS:-', 1)[1].split('}"', 1)[0]
    waits = [int(d) for d in default.split()]
    assert 3 <= len(waits) <= 8 and 120 <= sum(waits) <= 600, waits


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.nvidia"])
def test_both_images_install_through_the_script(dockerfile: str) -> None:
    text = (DOCKER / dockerfile).read_text(encoding="utf-8")
    assert "COPY docker/abstractgateway-server/install-gateway.sh /usr/local/bin/abstractgateway-install" in text
    assert "&& sh /usr/local/bin/abstractgateway-install" in text
    # No second, unretried install of the PyPI version beside it.
    assert 'pip install "abstractgateway' not in text
    for arg in ("ARG ABSTRACTGATEWAY_VERSION", "ARG ABSTRACTGATEWAY_INSTALL_MODE=pypi", "ARG ABSTRACTGATEWAY_EXTRAS="):
        assert arg in text, arg
