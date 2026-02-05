from __future__ import annotations

import sys
import types

import pytest


@pytest.mark.basic
def test_build_uvicorn_log_config_keeps_access_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.cli import _build_uvicorn_log_config

    abstractcore_mod = types.ModuleType("abstractcore")
    abstractcore_utils_mod = types.ModuleType("abstractcore.utils")
    structured_logging = types.ModuleType("abstractcore.utils.structured_logging")

    class ColoredFormatter:  # noqa: D401
        """Test stub."""

    structured_logging.ColoredFormatter = ColoredFormatter  # type: ignore[attr-defined]
    abstractcore_mod.utils = abstractcore_utils_mod  # type: ignore[attr-defined]
    abstractcore_utils_mod.structured_logging = structured_logging  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "abstractcore", abstractcore_mod)
    monkeypatch.setitem(sys.modules, "abstractcore.utils", abstractcore_utils_mod)
    monkeypatch.setitem(sys.modules, "abstractcore.utils.structured_logging", structured_logging)

    base = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"()": "uvicorn.logging.DefaultFormatter", "fmt": "%(message)s", "use_colors": None},
            "access": {"()": "uvicorn.logging.AccessFormatter", "fmt": "%(message)s", "use_colors": None},
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stderr"},
            "access": {"class": "logging.StreamHandler", "formatter": "access", "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn.error": {"handlers": ["default"], "level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }
    uvicorn_stub = types.SimpleNamespace(config=types.SimpleNamespace(LOGGING_CONFIG=base))

    cfg = _build_uvicorn_log_config(uvicorn=uvicorn_stub, silence_gpu_metrics_access_log=False)
    assert cfg["formatters"]["access"]["()"] == "uvicorn.logging.AccessFormatter"
