"""Architectural boundary guard (backlog 0059).

Gateway's differentiator is clean layering: it imports AbstractRuntime and never
AbstractCore directly. AbstractCore config/LLM/tool access must flow through
Runtime facades. This test walks the Gateway source AST and fails on any direct
`abstractcore` import so the boundary cannot silently regress (it did once, in
0.2.26/0.2.27, after backlog 0050 declared it clean).

If a new need arises that has no Runtime facade, the fix is to add the facade in
AbstractRuntime (mirroring `config_facade.py`), not to import AbstractCore here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "abstractgateway"


def _python_files() -> list[Path]:
    return sorted(p for p in _SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _abstractcore_imports(path: Path) -> list[str]:
    """Return human-readable descriptions of any abstractcore import in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "abstractcore" or alias.name.startswith("abstractcore."):
                    offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "abstractcore" or module.startswith("abstractcore."):
                names = ", ".join(a.name for a in node.names)
                offenders.append(f"{path.name}:{node.lineno} from {module} import {names}")
    return offenders


def test_gateway_source_does_not_import_abstractcore_directly() -> None:
    offenders: list[str] = []
    for path in _python_files():
        offenders.extend(_abstractcore_imports(path))
    assert not offenders, (
        "Gateway source must not import `abstractcore` directly; route AbstractCore "
        "access through an AbstractRuntime facade. Offending imports:\n  "
        + "\n  ".join(offenders)
    )


def test_config_facade_is_the_capability_defaults_path() -> None:
    """The capability-defaults module must use the Runtime config facade."""
    text = (_SRC_ROOT / "capability_defaults.py").read_text(encoding="utf-8")
    assert "from abstractruntime.integrations.abstractcore import config_facade" in text
    assert "ConfigurationManager" not in text, (
        "capability_defaults.py must not reference AbstractCore's ConfigurationManager; "
        "use config_facade.* instead."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
