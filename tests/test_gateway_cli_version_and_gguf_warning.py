from __future__ import annotations

import importlib.util
import platform
import sys
import types
import warnings

import pytest

import abstractgateway
from abstractgateway import cli


def test_version_flag_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"abstractgateway {abstractgateway.__version__}"


def _fake_core(monkeypatch: pytest.MonkeyPatch, answer: bool) -> list[int]:
    calls: list[int] = []
    fake = types.ModuleType("abstractcore")

    def _reserve() -> bool:
        calls.append(1)
        return answer

    fake.enable_gguf_metal = _reserve  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "abstractcore", fake)
    return calls


def _host(monkeypatch: pytest.MonkeyPatch, system: str, machine: str, llama_cpp: bool) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    real_find_spec = importlib.util.find_spec

    def _find_spec(name: str, *a, **k):
        if name == "llama_cpp":
            return object() if llama_cpp else None
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)


@pytest.mark.parametrize(
    "system,machine,llama_cpp",
    [("Darwin", "arm64", False), ("Linux", "x86_64", True), ("Darwin", "x86_64", True)],
)
def test_no_pytorch_warning_where_metal_offload_cannot_exist(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, llama_cpp: bool
) -> None:
    # A light install (no llama-cpp-python) or a non-Apple-silicon host: the
    # reservation is a no-op, so no "PyTorch was imported" warning.
    calls = _fake_core(monkeypatch, answer=False)
    _host(monkeypatch, system, machine, llama_cpp)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        cli._reserve_gguf_metal()
    assert not [w for w in seen if "GGUF GPU offload" in str(w.message)]
    assert calls == []


def test_warning_kept_when_llama_cpp_is_installed_on_apple_silicon(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_core(monkeypatch, answer=False)
    _host(monkeypatch, "Darwin", "arm64", True)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        cli._reserve_gguf_metal()
    assert calls == [1]
    assert [w for w in seen if "GGUF GPU offload could NOT be reserved" in str(w.message)]
