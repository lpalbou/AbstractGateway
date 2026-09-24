"""A requested gateway restart does not carry in-process Hub offline flags into
the new process (mission V, 2026-09-24).

`relaunch_process` replaces the gateway with a fresh one. It used to call
`os.execv`, which hands the new process the CURRENT `os.environ` -- so an
`HF_HUB_OFFLINE=1` written in-process during the run (the old MLX / HF
provider writes, a third-party library) became the new process's start-up
environment, which AbstractCore then records as the OPERATOR's choice and
which makes every explicit download refuse by name.

Pins (exec / spawn replaced by fakes that capture the environment):
- an offline flag written in-process is dropped, and the drop is logged by name;
- a flag the operator set before start survives, even when it was removed or
  changed in-process (it is restored to the operator's value);
- every other variable passes through;
- the Windows spawn branch gets the same environment;
- a flag present in only ONE of the two start-up snapshots (this module's and
  AbstractCore's) is not treated as the operator's.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from abstractgateway import host_control

TRIO = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")


class _Exec(Exception):
    """Raised by the fake exec so `relaunch_process` returns to the test."""


@pytest.fixture
def captured(monkeypatch):
    calls: List[Dict[str, Any]] = []

    def fake_execve(path, argv, env):
        calls.append({"how": "execve", "path": path, "argv": list(argv), "env": dict(env)})
        raise _Exec()

    def fake_execv(path, argv):  # the old call: must not be used any more
        calls.append({"how": "execv", "path": path, "argv": list(argv), "env": None})
        raise _Exec()

    monkeypatch.setattr(host_control.os, "execve", fake_execve)
    monkeypatch.setattr(host_control.os, "execv", fake_execv)
    return calls


def _start_snapshot(monkeypatch, gateway: Dict[str, Optional[str]], core: Optional[Dict[str, Optional[str]]] = None):
    """Pretend the gateway (and optionally AbstractCore) started with these values."""
    monkeypatch.setattr(host_control, "_START_HF_OFFLINE_ENV", {n: gateway.get(n) for n in TRIO})
    if core is None:
        monkeypatch.delitem(sys.modules, "abstractcore.config.manager", raising=False)
    else:
        fake = types.ModuleType("abstractcore.config.manager")
        fake.operator_hf_offline_env = lambda: {n: core.get(n) for n in TRIO}  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "abstractcore.config.manager", fake)


def _live(monkeypatch, **values: Optional[str]):
    for name in TRIO:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        if value is not None:
            monkeypatch.setenv(name, value)


def test_in_process_offline_flags_are_not_passed_to_the_relaunched_gateway(monkeypatch, captured, caplog):
    _start_snapshot(monkeypatch, gateway={}, core={})
    _live(monkeypatch, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
    monkeypatch.setenv("MISSION_V_KEEP_ME", "yes")

    with caplog.at_level(logging.WARNING, logger="abstractgateway.host_control"), pytest.raises(_Exec):
        host_control.relaunch_process(["/py", "-m", "abstractgateway", "serve"])

    assert len(captured) == 1 and captured[0]["how"] == "execve"
    env = captured[0]["env"]
    for name in TRIO:
        assert name not in env, name
    assert env["MISSION_V_KEEP_ME"] == "yes"
    assert captured[0]["argv"] == ["/py", "-m", "abstractgateway", "serve"]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "HF_HUB_OFFLINE: 1 -> <unset>" in logged and "TRANSFORMERS_OFFLINE: 1 -> <unset>" in logged


def test_an_operator_set_flag_survives_and_is_restored_if_changed_in_process(monkeypatch, captured):
    _start_snapshot(monkeypatch, gateway={"HF_HUB_OFFLINE": "1"}, core={"HF_HUB_OFFLINE": "1"})
    # in-process: someone lifted it, and wrote another one
    _live(monkeypatch, TRANSFORMERS_OFFLINE="1")

    with pytest.raises(_Exec):
        host_control.relaunch_process(["/py", "-m", "abstractgateway", "serve"])

    env = captured[0]["env"]
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "TRANSFORMERS_OFFLINE" not in env


def test_nothing_changes_when_nothing_was_written_in_process(monkeypatch, captured, caplog):
    _start_snapshot(monkeypatch, gateway={"HF_HUB_OFFLINE": "0"}, core={"HF_HUB_OFFLINE": "0"})
    _live(monkeypatch, HF_HUB_OFFLINE="0")
    env, changed = host_control.relaunch_env()
    assert changed == {}
    assert env["HF_HUB_OFFLINE"] == "0"


def test_the_windows_spawn_branch_gets_the_same_environment(monkeypatch, captured):
    _start_snapshot(monkeypatch, gateway={}, core={})
    _live(monkeypatch, HF_HUB_OFFLINE="1")
    spawned: List[Dict[str, Any]] = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            spawned.append({"cmd": list(cmd), **kwargs})

    def fake_exit(code):
        raise _Exec()

    monkeypatch.setattr(host_control.os, "name", "nt")
    monkeypatch.setattr(host_control.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(host_control.os, "_exit", fake_exit)

    with pytest.raises(_Exec):
        host_control.relaunch_process(["C:/py.exe", "-m", "abstractgateway", "serve"])

    assert captured == []  # no exec on Windows
    assert len(spawned) == 1
    assert spawned[0]["env"] is not None and "HF_HUB_OFFLINE" not in spawned[0]["env"]


def test_a_flag_only_one_start_snapshot_saw_is_not_the_operators(monkeypatch):
    # AbstractCore was imported AFTER something wrote the flag: it saw 1, this
    # module (imported earlier) saw nothing -- the flag is an in-process write.
    _start_snapshot(monkeypatch, gateway={}, core={"HF_HUB_OFFLINE": "1"})
    _live(monkeypatch, HF_HUB_OFFLINE="1")
    env, changed = host_control.relaunch_env()
    assert "HF_HUB_OFFLINE" not in env
    assert changed == {"HF_HUB_OFFLINE": "1 -> <unset>"}


def test_abstractcore_is_not_imported_just_to_read_its_snapshot(monkeypatch):
    _start_snapshot(monkeypatch, gateway={"HF_HUB_OFFLINE": "1"}, core=None)
    _live(monkeypatch, HF_HUB_OFFLINE="1")
    env, changed = host_control.relaunch_env()
    assert "abstractcore.config.manager" not in sys.modules
    assert env["HF_HUB_OFFLINE"] == "1" and changed == {}
