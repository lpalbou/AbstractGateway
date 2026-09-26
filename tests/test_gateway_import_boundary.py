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


# Process-bootstrap exemptions, named one by one. `cli._reserve_gguf_metal`
# must run before anything imports torch (see its docstring); it touches no
# AbstractCore config/LLM/tool surface, and AbstractRuntime has no facade for
# the GPU reservation yet. Any other direct import stays RED.
#
# The tray's About (tray/app.py) reads the AbstractFramework identity through
# `abstractcore.utils.identity` (CONTRACTS §B names it as THE shared Python
# API for the descriptor): presentation text only, no config/LLM/tool surface.
_BOOTSTRAP_EXEMPTIONS = frozenset({
    ("cli.py", "import abstractcore"),
    ("app.py", "from abstractcore.utils.identity import about_lines, app_identity, gateway_version_rows"),
})


def _is_exempt(offender: str) -> bool:
    # offender format: "<file>:<lineno> <import statement>"
    location, _, statement = offender.partition(" ")
    return (location.split(":", 1)[0], statement) in _BOOTSTRAP_EXEMPTIONS


def test_gateway_source_does_not_import_abstractcore_directly() -> None:
    offenders: list[str] = []
    for path in _python_files():
        offenders.extend(o for o in _abstractcore_imports(path) if not _is_exempt(o))
    assert not offenders, (
        "Gateway source must not import `abstractcore` directly; route AbstractCore "
        "access through an AbstractRuntime facade. Offending imports:\n  "
        + "\n  ".join(offenders)
    )


_SEAM_MODULE = "core_config.py"


def test_config_facade_is_the_core_config_seam_path() -> None:
    """The seam must use the Runtime config facade, not AbstractCore directly."""
    text = (_SRC_ROOT / _SEAM_MODULE).read_text(encoding="utf-8")
    assert "from abstractruntime.integrations.abstractcore import config_facade" in text
    assert "ConfigurationManager" not in text, (
        f"{_SEAM_MODULE} must not reference AbstractCore's ConfigurationManager; "
        "use config_facade.* instead."
    )


# The AbstractCore config facade is also re-exported FLAT from the integration
# package (`from abstractruntime.integrations.abstractcore import
# set_capability_default`). That spelling reaches the same store without ever
# naming `config_facade`, so it is a door too and is named here.
_FLAT_CONFIG_REEXPORTS = frozenset(
    {
        "capability_default_config_file",
        "capability_default_specs",
        "clear_capability_default",
        "list_capability_defaults",
        "read_config_api_key",
        "set_capability_default",
        "update_capability_default",
    }
)

# The seam's own write/read surface. A second Gateway module exporting these
# names is a re-export shim, which is how "one door" quietly becomes two.
_SEAM_EXPORTS = frozenset(
    {
        "gateway_capability_defaults_payload",
        "save_gateway_capability_default",
        "clear_gateway_capability_default",
        "capability_defaults_config_signature",
        "text_default",
        "reasoning_default",
        "read_core_config_api_key",
        # The weights half of the same door: availability probes and the
        # download verb are AbstractCore's materializer, reached only here.
        # A Gateway module that re-exported these could grow its own
        # `shutil.which("ollama")` next to them and nobody would notice.
        "gateway_model_availability_payload",
        "core_model_download",
        "recommended_core_model_downloads",
        # Models & engines (AbstractCore >= 2.14.0): host profile, engines,
        # catalog, installed models, deletes, host jobs, console screens.
        "core_models_engines_support",
        "core_host_profile",
        "core_engine_inventory",
        "core_engine_status",
        "core_engine_install_plan",
        "core_engine_download_url",
        "core_engine_install",
        "core_model_catalog",
        "core_installed_models",
        "core_model_delete",
        "core_start_model_download",
        "core_host_jobs",
        "core_host_job",
        "core_host_job_cancel",
        "core_console_fragment",
    }
)


def _config_facade_importers(path: Path) -> list[str]:
    """Every way `path` could reach AbstractCore-owned config off the seam."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [a.name for a in node.names]
            if module == "abstractruntime.integrations.abstractcore":
                if "config_facade" in names:
                    offenders.append(f"{path.name}:{node.lineno} from {module} import config_facade")
                flat = sorted(set(names) & _FLAT_CONFIG_REEXPORTS)
                if flat:
                    offenders.append(f"{path.name}:{node.lineno} from {module} import {', '.join(flat)}")
            elif module.endswith("integrations.abstractcore.config_facade"):
                offenders.append(f"{path.name}:{node.lineno} from {module} import {', '.join(names)}")
            elif module == "abstractruntime.integrations" and "abstractcore" in names:
                # The package binding alone is enough to reach `.config_facade`.
                offenders.append(f"{path.name}:{node.lineno} from {module} import abstractcore")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("integrations.abstractcore.config_facade"):
                    offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Attribute):
            if node.attr == "config_facade":
                offenders.append(f"{path.name}:{node.lineno} attribute access .config_facade")
        elif isinstance(node, ast.Constant):
            # `importlib.import_module("...config_facade")` and friends.
            if isinstance(node.value, str) and node.value.endswith("integrations.abstractcore.config_facade"):
                offenders.append(f"{path.name}:{node.lineno} dynamic import of {node.value}")
    return offenders


_SUBPROCESS_CALLEES = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "system", "popen", "getoutput", "create_subprocess_exec"}
)


def _abstractcore_cli_shellouts(path: Path) -> list[str]:
    """Any shell-out to the `abstractcore config` CLI, which writes the same store.

    Prose that merely NAMES the CLI (a docstring pointing an operator at it, a
    log line explaining who else wrote the file) is not a door; only a process
    launch is, so this looks at call sites rather than at lines of text.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
        if name not in _SUBPROCESS_CALLEES:
            continue
        literals = [
            sub.value for sub in ast.walk(node) if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        ]
        joined = " ".join(literals)
        if "abstractcore config" in joined or ("abstractcore" in literals and "config" in literals):
            offenders.append(f"{path.name}:{node.lineno} shells out to the abstractcore config CLI")
    return offenders


def test_core_owned_config_has_exactly_one_door() -> None:
    """`core_config.py` is the only Gateway module that reaches AbstractCore config.

    AbstractCore-owned configuration (capability routes, the reasoning effort on
    the text route, route options, Core-held provider API keys) has one source of
    truth. A second module reading or writing it -- through the facade, through
    the integration package's flat re-exports, through a dynamic import, or by
    shelling out to `abstractcore config` -- is how a Gateway-side copy starts,
    so every one of those spellings is named here: route the need through
    `core_config` instead of adding an import.
    """
    offenders: list[str] = []
    for path in _python_files():
        if path.name == _SEAM_MODULE:
            continue
        offenders.extend(_config_facade_importers(path))
        offenders.extend(_abstractcore_cli_shellouts(path))
    assert not offenders, (
        "AbstractCore-owned configuration must be reached through "
        f"`abstractgateway.{_SEAM_MODULE[:-3]}`, the one seam. Offending imports:\n  "
        + "\n  ".join(offenders)
    )


def test_no_second_module_re_exports_the_seam() -> None:
    """No Gateway module may re-export the seam's surface under another name.

    A compatibility shim that forwards `save_gateway_capability_default` is a
    second door with a friendly face: new code can import it, never touch
    `core_config`, and the door count silently goes from one to two.
    """
    offenders: list[str] = []
    for path in _python_files():
        if path.name == _SEAM_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if not (module == "core_config" or module.endswith(".core_config") or module == ".core_config"):
                continue
            if node.level and not module:
                continue
            reexported = sorted({a.asname or a.name for a in node.names} & _SEAM_EXPORTS)
            if reexported and node.col_offset == 0:
                # A module-level import into a module that also declares them in
                # `__all__` is a shim; a plain module-level use is fine.
                text = path.read_text(encoding="utf-8")
                if all(f'"{name}"' in text.split("__all__", 1)[-1] for name in reexported) and "__all__" in text:
                    offenders.append(f"{path.name}:{node.lineno} re-exports {', '.join(reexported)}")
    assert not offenders, (
        "The seam has exactly one door. Do not re-export it:\n  " + "\n  ".join(offenders)
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
