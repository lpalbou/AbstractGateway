"""The workflow folder a data folder remembers (`workflow_folder.py`): the process's `--flows-dir`
(`workflow_folder.chosen`), else the remembered folder, else the folder next to the running code; the
service remembers what it uses. The old flows-dir variables are not read."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from abstractgateway import config as cfg_mod
from abstractgateway import service as svc_mod
from abstractgateway import workflow_folder as wf

VARIABLES = ("ABSTRACTGATEWAY_FLOWS_DIR", "ABSTRACTFRAMEWORK_WORKFLOWS_DIR", "ABSTRACTFLOW_FLOWS_DIR")
REAL_BASIC_AGENT = Path(cfg_mod._default_flows_dir()) / "basic-agent.flow"


def _folder(path: Path) -> Path:
    path.mkdir(parents=True)
    shutil.copy2(REAL_BASIC_AGENT, path / "basic-agent.flow")
    return path.resolve()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh data folder, no `--flows-dir`, and code whose own folder is B (the shipped shape)."""
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(wf, "chosen", None)
    monkeypatch.setattr(wf, "serving", {})
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    code_b = _folder(tmp_path / "B" / "flows" / "bundles")
    monkeypatch.setattr(cfg_mod, "_default_flows_dir", lambda: str(code_b))
    return data, code_b, tmp_path


def _build_until_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_default_gateway_service` up to the stores (after the folder is remembered)."""

    class Stop(Exception):
        pass

    def stop(**_k):
        raise Stop

    monkeypatch.setattr(svc_mod, "build_file_stores", stop)
    with pytest.raises(Stop):
        svc_mod.create_default_gateway_service()


def test_the_remembered_folder_wins_over_the_folder_next_to_the_code(env) -> None:
    data, code_b, tmp = env
    a = _folder(tmp / "A")
    wf.remember(data, a)
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == a  # not B: the incident of 2026-11-07


def test_the_flag_wins_becomes_remembered_and_a_later_start_without_it_keeps_it(env, monkeypatch: pytest.MonkeyPatch) -> None:
    data, code_b, tmp = env
    wf.remember(data, _folder(tmp / "A"))
    c = _folder(tmp / "C")
    monkeypatch.setattr(wf, "chosen", c)  # `serve --flows-dir C`
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == c
    _build_until_remembered(monkeypatch)
    assert wf.remembered(data) == c
    monkeypatch.setattr(wf, "chosen", None)  # the next start, without the flag
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == c


def test_the_old_variables_change_nothing(env, monkeypatch: pytest.MonkeyPatch) -> None:
    data, code_b, tmp = env
    for name in VARIABLES:
        monkeypatch.setenv(name, str(_folder(tmp / name)))
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == code_b
    a = _folder(tmp / "A")
    wf.remember(data, a)
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == a
    _build_until_remembered(monkeypatch)
    assert wf.remembered(data) == a


@pytest.mark.parametrize("command", ["serve", "runner"])
def test_serve_and_runner_take_an_optional_flows_dir(command: str, capsys: pytest.CaptureFixture) -> None:
    from abstractgateway import cli

    with pytest.raises(SystemExit) as e:
        cli.main([command, "--help"])
    assert e.value.code == 0
    assert "--flows-dir FOLDER" in capsys.readouterr().out


def test_a_remembered_folder_missing_for_one_start_is_kept_and_served_again_once_back(env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    data, code_b, tmp = env
    a = _folder(tmp / "A")
    wf.remember(data, a)
    before = wf.record_path(data).read_bytes()
    a.rename(tmp / "A-away")  # A is away for one start
    assert wf.remembered(data) is None
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == code_b
    with caplog.at_level("ERROR", logger=wf.__name__):
        _build_until_remembered(monkeypatch)
    assert wf.record_path(data).read_bytes() == before  # the record is kept
    lines = [r.getMessage() for r in caplog.records if r.name == wf.__name__]
    assert len(lines) == 1 and str(a) in lines[0] and str(code_b) in lines[0], lines
    (tmp / "A-away").rename(a)  # A is back: the next start (a new process) serves it
    monkeypatch.setattr(wf, "serving", {})
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == a


def test_an_unreadable_record_is_kept_at_the_start(env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    data, code_b, _tmp = env
    wf.record_path(data).parent.mkdir(parents=True)
    wf.record_path(data).write_bytes(b"{not json")
    with caplog.at_level("ERROR", logger=wf.__name__):
        _build_until_remembered(monkeypatch)
    assert wf.record_path(data).read_bytes() == b"{not json"
    assert len([r for r in caplog.records if r.name == wf.__name__]) == 1


def test_a_process_keeps_the_folder_it_serves_when_another_process_records_another(env, monkeypatch: pytest.MonkeyPatch) -> None:
    data, code_b, tmp = env
    a = _folder(tmp / "A")
    wf.remember(data, a)
    _build_until_remembered(monkeypatch)
    wf.remember(data, _folder(tmp / "other"))  # another process started with `--flows-dir other`
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == a  # a user plane built later mounts A


def test_serve_flows_dir_creates_a_missing_folder_before_remembering_it(env, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli

    data, _code_b, tmp = env
    new = tmp / "not" / "yet"

    class Stop(Exception):
        pass

    def stop() -> None:
        raise Stop

    monkeypatch.setattr(cli, "_migrate_legacy_core_config_store", stop)  # the first step of `serve` after the flag
    with pytest.raises(Stop):
        cli.main(["serve", "--flows-dir", str(new), "--data-dir", str(data), "--no-tray"])
    assert new.is_dir()
    assert wf.remembered(data) == new.resolve()  # a `--reload` child reads this record


def test_a_relative_path_in_the_record_is_not_accepted(env, monkeypatch: pytest.MonkeyPatch) -> None:
    data, code_b, tmp = env
    (tmp / "rel").mkdir()
    monkeypatch.chdir(tmp)
    wf.record_path(data).parent.mkdir(parents=True)
    wf.record_path(data).write_text(json.dumps({"folder": "rel"}), encoding="utf-8")
    assert wf.remembered(data) is None


def test_the_remembered_path_is_stored_resolved(env) -> None:
    data, _code_b, tmp = env
    b = _folder(tmp / "resolved")
    wf.remember(data, tmp / "x" / ".." / "resolved")
    assert json.loads(wf.record_path(data).read_text(encoding="utf-8")) == {"folder": str(b)}


def test_remember_on_a_read_only_data_folder_does_not_raise(env, tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        wf.remember(ro, tmp_path)
        assert not wf.record_path(ro).exists()
    finally:
        ro.chmod(0o700)


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe", b'["a"]', b'{"folder": 3}', b'{"folder": "relative/path"}'])
def test_an_unreadable_record_means_the_default(env, content: bytes) -> None:
    data, code_b, _tmp = env
    wf.record_path(data).parent.mkdir(parents=True)
    wf.record_path(data).write_bytes(content)
    assert wf.remembered(data) is None
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == code_b


def test_a_reader_never_creates_or_changes_the_record(env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    data, code_b, tmp = env
    from abstractgateway import config_cli

    cfg_mod.GatewayHostConfig.from_env()
    config_cli.main(["status", "--json"])
    assert json.loads(capsys.readouterr().out)["gateway"]["flows_dir"] == str(code_b)
    assert not wf.record_path(data).exists()

    a = _folder(tmp / "A")
    wf.remember(data, a)
    before = wf.record_path(data).read_bytes()
    monkeypatch.setattr(wf, "chosen", _folder(tmp / "C"))
    assert cfg_mod.GatewayHostConfig.from_env().flows_dir == tmp.resolve() / "C"
    config_cli.main(["status"])
    assert wf.record_path(data).read_bytes() == before


def test_the_first_build_with_nothing_remembers_the_default(env, monkeypatch: pytest.MonkeyPatch) -> None:
    data, code_b, _tmp = env
    _build_until_remembered(monkeypatch)
    assert json.loads(wf.record_path(data).read_text(encoding="utf-8")) == {"folder": str(code_b)}
    mtime = wf.record_path(data).stat().st_mtime_ns
    wf.remember(data, code_b)  # the same value is not written again
    assert wf.record_path(data).stat().st_mtime_ns == mtime


def test_config_init_remembers_its_flows_dir_for_its_data_dir_and_writes_no_env_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import config_cli

    monkeypatch.chdir(tmp_path)
    flows = tmp_path / "X"
    flows.mkdir()
    config_cli.main(["init", "--env-file", "with.env", "--data-dir", "D", "--flows-dir", str(flows)])
    assert wf.remembered(tmp_path / "D") == flows.resolve()
    assert "FLOWS" not in (tmp_path / "with.env").read_text(encoding="utf-8")

    config_cli.main(["init", "--env-file", "without.env", "--data-dir", "E"])
    assert not wf.record_path(tmp_path / "E").exists()
    assert "FLOWS" not in (tmp_path / "without.env").read_text(encoding="utf-8")
