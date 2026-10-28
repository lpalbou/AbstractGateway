"""Runtime twin of the AST boundary rule (review G4): import the gateway app under an import hook that records
every `abstractmemory` import whose IMPORTER is a gateway module, whatever its spelling (`import`, `from`,
`__import__`, `importlib.import_module`, a computed name). AbstractRuntime's memory facade importing
AbstractMemory on the gateway's behalf is the allowed path; a gateway module doing it itself is not.

Scope: module-level code (what runs at import). Function-level imports are the AST test's job
(`test_gateway_import_boundary.py`); this one catches the dynamic spellings that walk cannot see."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

_SCRIPT = r'''
import builtins, importlib, json, sys

offenders = []
_real_import = builtins.__import__
_real_import_module = importlib.import_module


def _check(name, importer):
    name = str(name or "")
    if (name == "abstractmemory" or name.startswith("abstractmemory.")) and str(importer or "").startswith("abstractgateway"):
        offenders.append(f"{importer}: {name}")


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0:
        # A bare __import__("x") call passes no globals: the importer is the calling frame.
        importer = (globals if globals is not None else sys._getframe(1).f_globals).get("__name__")
        _check(name, importer)
    return _real_import(name, globals, locals, fromlist, level)


def _guarded_import_module(name, package=None):
    _check(name, sys._getframe(1).f_globals.get("__name__"))
    return _real_import_module(name, package)


builtins.__import__ = _guarded_import
importlib.import_module = _guarded_import_module

# The hook sees each spelling (self-check: a guard that sees nothing proves nothing).
exec("import abstractmemory", {"__name__": "abstractgateway._probe_a"})
exec("from abstractmemory import graph", {"__name__": "abstractgateway._probe_b"})
exec("import importlib\nimportlib.import_module('abstract' + 'memory')", {"__name__": "abstractgateway._probe_c"})
exec("__import__('abstractmemory.graph')", {"__name__": "abstractgateway._probe_d", "__builtins__": builtins})
probes = sorted(offenders)
offenders.clear()

import abstractgateway.app  # noqa: E402,F401  (mounts every router)
import abstractgateway.routes.memory  # noqa: E402,F401
import abstractgateway.memory_store  # noqa: E402,F401
import abstractgateway.entities  # noqa: E402,F401
import abstractgateway.entity_gate  # noqa: E402,F401
import abstractgateway.entity_replay  # noqa: E402,F401
import abstractgateway.template_store  # noqa: E402,F401

print(json.dumps({"probes": probes, "offenders": offenders, "am_loaded": "abstractmemory" in sys.modules}))
'''


def test_no_gateway_module_imports_abstractmemory_at_runtime(tmp_path: Path) -> None:
    env = dict(os.environ)
    # The child resolves the same packages as this process (a relative PYTHONPATH must not change meaning).
    env["PYTHONPATH"] = os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p and Path(p).exists())
    env["ABSTRACTGATEWAY_DATA_DIR"] = str(tmp_path / "runtime")
    env["ABSTRACTGATEWAY_FLOWS_DIR"] = str(tmp_path / "flows")
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=300
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["probes"] == [
        "abstractgateway._probe_a: abstractmemory",
        "abstractgateway._probe_b: abstractmemory",
        "abstractgateway._probe_c: abstractmemory",
        "abstractgateway._probe_d: abstractmemory.graph",
    ], result
    assert result["offenders"] == [], (
        "gateway modules imported AbstractMemory directly at runtime (use the AbstractRuntime memory facade): "
        f"{result['offenders']}"
    )
    # The facade did import it on the gateway's behalf (entity_gate's module-level budgets): the hook was live.
    assert result["am_loaded"] is True
