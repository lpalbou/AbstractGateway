"""The local gateway pointer writer (root backlog 0943, gateway_pointer.py):
`serve` writes ~/.abstractframework/gateway.json once bound, under the
ownership rule; no token; atomic, 0600. Every test uses a scratch HOME
(conftest) and explicit paths: the real pointer is never touched."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from abstractgateway.gateway_pointer import (
    gateway_pointer_path,
    read_gateway_pointer,
    record_serve_pointer,
    serve_owns_pointer,
    write_gateway_pointer,
)


def test_the_pointer_lives_under_the_home(tmp_path: Path) -> None:
    assert gateway_pointer_path(tmp_path) == tmp_path / ".abstractframework" / "gateway.json"
    from conftest import _REAL_HOME

    assert not str(gateway_pointer_path()).startswith(_REAL_HOME + os.sep), "conftest must isolate HOME"


def test_write_is_the_contract_shape_private_and_tokenless(tmp_path: Path) -> None:
    p = tmp_path / "home" / ".abstractframework" / "gateway.json"
    write_gateway_pointer(url="http://127.0.0.1:8081", port=8081, data_dir=tmp_path / "data", written_by="serve", path=p)
    data = json.loads(p.read_text())
    assert set(data) == {"schema", "url", "port", "data_dir", "updated_at", "written_by"}
    assert data["schema"] == 1 and data["url"] == "http://127.0.0.1:8081" and data["port"] == 8081
    assert data["data_dir"] == str((tmp_path / "data").resolve()) and data["written_by"] == "serve"
    assert data["updated_at"].endswith("Z")
    if sys.platform != "win32":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [x.name for x in p.parent.iterdir()] == ["gateway.json"], "no temp file left behind"
    with pytest.raises(ValueError):
        write_gateway_pointer(url="http://192.168.1.5:8081", port=8081, data_dir=tmp_path, written_by="serve", path=p)


def test_ownership_rule(tmp_path: Path) -> None:
    default = tmp_path / "default"
    other = tmp_path / "other"
    assert serve_owns_pointer(default, existing=None, default_data_dir=default)[0] is True
    assert serve_owns_pointer(other, existing=None, default_data_dir=default)[0] is False
    mine = {"data_dir": str(other)}
    assert serve_owns_pointer(other, existing=mine, default_data_dir=default)[0] is True
    assert serve_owns_pointer(default, existing=mine, default_data_dir=default)[0] is False, "the installer's custom data dir owns it"


def test_record_serve_pointer_two_gateways(tmp_path: Path) -> None:
    """The real gateway writes; a second gateway with its own data dir (the
    hermetic console, a pytest run) starts and never changes the file."""
    p = tmp_path / "home" / ".abstractframework" / "gateway.json"
    default = tmp_path / "default-data"
    written, why = record_serve_pointer(host="0.0.0.0", port=8081, data_dir=default, path=p, default_data_dir=default)
    assert written, why
    before = p.read_text()
    written, why = record_serve_pointer(host="127.0.0.1", port=18850, data_dir=tmp_path / "hermetic", path=p, default_data_dir=default)
    assert not written and "belongs to" in why
    assert p.read_text() == before
    # An admin moved the port and restarted: the owner rewrites.
    written, _ = record_serve_pointer(host="127.0.0.1", port=8095, data_dir=default, path=p, default_data_dir=default)
    assert written and read_gateway_pointer(p)["url"] == "http://127.0.0.1:8095"
    # A gateway bound to one LAN address only is not reachable on loopback.
    written, why = record_serve_pointer(host="192.168.1.5", port=8081, data_dir=default, path=p, default_data_dir=default)
    assert not written and "loopback" in why


def test_an_unreadable_pointer_is_rewritten_by_the_default_gateway_only(tmp_path: Path) -> None:
    p = tmp_path / ".abstractframework" / "gateway.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    default = tmp_path / "d"
    assert record_serve_pointer(host="127.0.0.1", port=9000, data_dir=tmp_path / "x", path=p, default_data_dir=default)[0] is False
    assert record_serve_pointer(host="127.0.0.1", port=9000, data_dir=default, path=p, default_data_dir=default)[0] is True


def test_serve_writes_the_pointer_after_bind_with_the_bound_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`abstractgateway serve` on the default data dir (scratch HOME): the
    pointer appears once uvicorn reports it is bound."""
    from test_gateway_cli_serve_host_controls import _FakeServer, _serve  # the fake-uvicorn harness

    from abstractgateway.host_paths import user_data_dir

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(user_data_dir(home=home)))
    import abstractgateway.gateway_pointer as gp

    started_at_write = []
    real_record = gp.record_serve_pointer

    def record(**kw):
        started_at_write.append(_FakeServer.instances[-1].started)
        return real_record(**kw)

    monkeypatch.setattr(gp, "record_serve_pointer", record)
    _serve(monkeypatch)
    for t in threading.enumerate():
        if t.name == "gateway-pointer":
            t.join(timeout=3.0)
    assert started_at_write == [True], "written once, never before the listener is bound"
    data = read_gateway_pointer(gateway_pointer_path(home))
    assert data is not None and data["url"] == "http://127.0.0.1:9999" and data["written_by"] == "serve"
    assert "token" not in json.dumps(data).lower()


def test_serve_on_another_data_dir_leaves_no_pointer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from test_gateway_cli_serve_host_controls import _serve

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "hermetic-data"))
    _serve(monkeypatch)
    for t in threading.enumerate():
        if t.name == "gateway-pointer":
            t.join(timeout=3.0)
    assert not gateway_pointer_path(home).exists()
