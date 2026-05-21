from __future__ import annotations

import types

import pytest


@pytest.mark.basic
def test_build_uvicorn_log_config_keeps_access_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.cli import _build_uvicorn_log_config

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
    assert cfg["formatters"]["default"]["()"] == "uvicorn.logging.DefaultFormatter"
    assert cfg["formatters"]["access"]["()"] == "uvicorn.logging.AccessFormatter"
