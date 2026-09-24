"""The subprocess guard in tests/conftest.py (mission FF, 2026-09-24).

A test that spawns the real `lms` / `ollama` CLI (or the desktop `open`)
escapes the socket guard: the child process talks to the live LM Studio /
Ollama servers or opens a window on the operator's screen. The conftest
refuses those launches unless the test is marked `desktop` / `network`, or
registered its own stand-in with the `fake_cli` fixture.

Every case uses a STAND-IN script that writes a sentinel file when it runs,
so "the guard is gone" shows up as the sentinel existing, never as a real
engine CLI being launched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _stand_in(bindir: Path, name: str, sentinel: Path) -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    path.write_text(f"#!/bin/sh\necho ran > {sentinel}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def stand_in_on_path(tmp_path, monkeypatch):
    """An UNREGISTERED stand-in `lms` / `ollama` / `open` first on PATH."""
    bindir = tmp_path / "bin"
    sentinel = tmp_path / "ran.txt"
    for name in ("lms", "ollama", "open"):
        _stand_in(bindir, name, sentinel)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return sentinel


def _refused(subprocess_guard, program: str) -> None:
    hits = list(subprocess_guard["hits"])
    subprocess_guard["hits"].clear()  # asserted here; the teardown must not fail the test for it
    assert hits and hits[-1]["program"] == program, hits


@pytest.mark.parametrize(
    "launch",
    [
        pytest.param(lambda: subprocess.run(["lms", "ls"], capture_output=True), id="run-argv"),
        pytest.param(lambda: subprocess.check_output(["ollama", "list"]), id="check_output"),
        pytest.param(lambda: subprocess.Popen(["open", "http://127.0.0.1:1"]).wait(), id="popen-open"),
        pytest.param(lambda: subprocess.run("ollama list", shell=True), id="shell-string"),
        pytest.param(lambda: subprocess.run(["/bin/sh", "-c", "cd /; lms server start"]), id="sh-c"),
        pytest.param(lambda: subprocess.run(["env", "FOO=1", "lms", "ls"]), id="env-wrapper"),
        pytest.param(lambda: os.system("lms ls"), id="os-system"),
    ],
)
def test_an_unregistered_engine_cli_is_refused_and_never_runs(launch, stand_in_on_path, subprocess_guard, request) -> None:
    refusal = None
    try:
        launch()
    except PermissionError as exc:
        refusal = exc
    assert not stand_in_on_path.exists(), "the stand-in CLI RAN: the subprocess guard did not stop it"
    assert refusal is not None and "subprocess guard" in str(refusal)
    _refused(subprocess_guard, {"run-argv": "lms", "check_output": "ollama", "popen-open": "open",
                                "shell-string": "ollama", "sh-c": "lms", "env-wrapper": "lms",
                                "os-system": "lms"}[request.node.callspec.id])


def test_a_registered_fake_cli_runs(fake_cli, tmp_path, subprocess_guard) -> None:
    sentinel = tmp_path / "fake-ran.txt"
    lms = fake_cli("lms", f"#!/bin/sh\necho \"$@\" > {sentinel}\n")
    out = subprocess.run([str(lms), "ls", "--json"], capture_output=True)
    assert out.returncode == 0 and sentinel.read_text().strip() == "ls --json"
    assert subprocess_guard["hits"] == []


def test_the_real_binary_is_refused_even_when_a_fake_was_registered(fake_cli, stand_in_on_path, subprocess_guard) -> None:
    fake_cli("lms")  # registers the fake's own path, not the name
    with pytest.raises(PermissionError):
        subprocess.run(["lms", "ls"])  # resolves to the unregistered stand-in on PATH
    assert not stand_in_on_path.exists()
    _refused(subprocess_guard, "lms")


def test_other_programs_are_untouched(tmp_path, subprocess_guard) -> None:
    out = subprocess.run(["/bin/sh", "-c", "echo ok"], capture_output=True, text=True)
    assert out.stdout.strip() == "ok" and subprocess_guard["hits"] == []
