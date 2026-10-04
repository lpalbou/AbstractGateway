"""The backlog folder and the exec runner as settings (mission II).

Operator 2026-09-24: "can you fix continuum for a new fresh / install ?
remember i don't like environment variables and these should be handled
with proper settings and --param_name."

Pins:
- ONE resolution (runtime_config.resolve_backlog_root / resolve_exec_runner):
  `serve --backlog-root` / `--exec-runner` > stored setting > (exec runner
  only: legacy env, `source: env`; round 8 removed the folder's env rung and
  stores an exported folder once) > default (<data dir>/backlog, created with the
  standard skeleton on first use; the runner off);
- every consumer honours it (the backlog routes read ONLY the environment
  before, so the stored setting was cosmetic and a fresh install had no
  backlog), and a vanished stored folder reports why;
- the doors: runtime-config POST, `abstractgateway config get|set|unset`,
  the serve launch flags — one validation;
- no `os.environ` write for the folder anywhere on the CLI paths.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

_TOKEN = "backlog-root-admin-secret"
_LEGACY_ENV = (
    "ABSTRACTGATEWAY_TRIAGE_REPO_ROOT",
    "ABSTRACT_TRIAGE_REPO_ROOT",
    "ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER",
    "ABSTRACT_BACKLOG_EXEC_RUNNER",
)


@pytest.fixture(autouse=True)
def _no_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LEGACY_ENV:
        monkeypatch.delenv(name, raising=False)


def _repo(root: Path, name: str) -> Path:
    p = root / name
    (p / "docs" / "backlog").mkdir(parents=True)
    return p


def _serve_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data = tmp_path / "runtime"
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    return data


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


# --------------------------------------------------------------- resolution


def test_resolution_order_flag_beats_stored_beats_default_no_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 8: flag > stored > the gateway's own folder. The environment is
    NOT a rung: a read (ensure=False, the settings GET) never reports it."""
    from abstractgateway.runtime_config import (
        clear_launch_settings,
        record_launch_settings,
        resolve_backlog_root,
        write_runtime_config,
    )

    data = tmp_path / "data"
    flag_repo, stored_repo, env_repo = _repo(tmp_path, "flag"), _repo(tmp_path, "stored"), _repo(tmp_path, "env")

    res = resolve_backlog_root(data)
    assert (res["source"], res["value"]) == ("default", str((data / "backlog").resolve()))

    # A GET-style read with the variable set: still the default, no env fields.
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(env_repo))
    res = resolve_backlog_root(data, ensure=False)
    assert (res["source"], res["value"]) == ("default", str((data / "backlog").resolve()))
    assert "env_name" not in res and "env_shadowed" not in res
    monkeypatch.delenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT")

    write_runtime_config(data, {"triage_repo_root": str(stored_repo)}, actor="t")
    res = resolve_backlog_root(data)
    assert (res["source"], res["value"]) == ("stored", str(stored_repo.resolve()))

    record_launch_settings(data, {"triage_repo_root": str(flag_repo)})
    res = resolve_backlog_root(data)
    assert (res["source"], res["value"]) == ("flag", str(flag_repo.resolve()))
    assert res["stored_value"] == str(stored_repo.resolve())  # the saved choice stays visible under the flag

    # A record written by a process that is gone is ignored.
    record_launch_settings(data, {"triage_repo_root": str(flag_repo)}, pid=2**22 + 12345)
    assert resolve_backlog_root(data)["source"] == "stored"

    clear_launch_settings(data)
    write_runtime_config(data, {"triage_repo_root": None}, actor="t")
    # Cleared with the old variable exported: the gateway's own folder, never the variable.
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(env_repo))
    assert resolve_backlog_root(data)["source"] == "default"


def test_legacy_env_backlog_folder_is_stored_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration: an install whose launcher exported the folder keeps it — the
    value is stored ONCE (first consumer read or serve start); afterwards the
    variable is ignored, and a clear is never undone by it."""
    import json as _json

    from abstractgateway.runtime_config import (
        migrate_legacy_backlog_root_env,
        read_runtime_config,
        resolve_backlog_root,
        write_runtime_config,
    )

    data = tmp_path / "data"
    env_repo, other = _repo(tmp_path, "env"), _repo(tmp_path, "other")
    monkeypatch.setenv("ABSTRACT_TRIAGE_REPO_ROOT", str(env_repo))
    res = resolve_backlog_root(data)  # a consumer read (ensure=True) migrates
    assert (res["source"], res["value"]) == ("stored", str(env_repo.resolve()))
    store = _json.loads((data / "config" / "runtime_config.json").read_text())
    assert store["triage_repo_root"] == str(env_repo.resolve())
    assert store["legacy_env_imported"]["triage_repo_root"]["env"] == "ABSTRACT_TRIAGE_REPO_ROOT"
    assert read_runtime_config(data)["triage_repo_root"]["source"] == "stored"

    # Once only: a changed variable is not imported again.
    monkeypatch.setenv("ABSTRACT_TRIAGE_REPO_ROOT", str(other))
    assert migrate_legacy_backlog_root_env(data) is False
    assert resolve_backlog_root(data)["value"] == str(env_repo.resolve())

    # Cleared by the admin: the variable never brings it back.
    write_runtime_config(data, {"triage_repo_root": None}, actor="t")
    assert resolve_backlog_root(data)["source"] == "default"

    # An invalid value is not stored (the gateway's own folder applies) and not retried.
    data2 = tmp_path / "data2"
    monkeypatch.setenv("ABSTRACT_TRIAGE_REPO_ROOT", str(tmp_path / "missing"))
    assert resolve_backlog_root(data2)["source"] == "default"
    store2 = _json.loads((data2 / "config" / "runtime_config.json").read_text())
    assert store2["legacy_env_imported"]["triage_repo_root"]["stored"] is False
    assert "triage_repo_root" not in store2

    # Kept as the old rung accepted it: an existing folder without docs/backlog (the process
    # manager's repo root) is stored too.
    data4 = tmp_path / "data4"
    plain = tmp_path / "plain-checkout"
    plain.mkdir()
    monkeypatch.setenv("ABSTRACT_TRIAGE_REPO_ROOT", str(plain))
    assert resolve_backlog_root(data4)["value"] == str(plain.resolve())

    # A folder saved before any import: the variable never replaces it, even after a clear.
    data3 = tmp_path / "data3"
    monkeypatch.delenv("ABSTRACT_TRIAGE_REPO_ROOT")
    write_runtime_config(data3, {"triage_repo_root": str(other)}, actor="t")
    monkeypatch.setenv("ABSTRACT_TRIAGE_REPO_ROOT", str(env_repo))
    write_runtime_config(data3, {"triage_repo_root": None}, actor="t")
    assert resolve_backlog_root(data3)["source"] == "default"


def test_serve_start_stores_the_legacy_env_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli

    data = tmp_path / "data"
    env_repo = _repo(tmp_path, "env")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(env_repo))
    cli._apply_backlog_launch_flags(argparse.Namespace(backlog_root=None, exec_runner=None), data)
    store = json.loads((data / "config" / "runtime_config.json").read_text())
    assert store["triage_repo_root"] == str(env_repo.resolve())


def test_exec_runner_order_flag_beats_stored_beats_env_beats_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.runtime_config import (
        read_runtime_config,
        record_launch_settings,
        resolve_backlog_exec_runner_enabled,
        resolve_exec_runner,
        write_runtime_config,
    )

    data = tmp_path / "data"
    assert resolve_exec_runner(data) == {"value": False, "source": "default"}
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER", "1")
    assert {k: resolve_exec_runner(data)[k] for k in ("value", "source")} == {"value": True, "source": "env"}
    write_runtime_config(data, {"backlog_exec_runner": "off"}, actor="t")
    assert {k: resolve_exec_runner(data)[k] for k in ("value", "source")} == {"value": False, "source": "stored"}
    record_launch_settings(data, {"backlog_exec_runner": True})
    assert {k: resolve_exec_runner(data)[k] for k in ("value", "source")} == {"value": True, "source": "flag"}
    assert resolve_exec_runner(data)["stored_value"] is False
    assert resolve_backlog_exec_runner_enabled(data) is True
    # The settings read serves the same answer with its labels.
    knob = read_runtime_config(data)["backlog_exec_runner"]
    assert (knob["value"], knob["source"], knob["label"]) == (True, "flag", "Backlog exec runner")


def test_default_folder_gets_the_standard_skeleton_on_first_use_only(tmp_path: Path) -> None:
    from importlib import resources

    from abstractgateway.runtime_config import read_runtime_config, resolve_backlog_root

    data = tmp_path / "data"
    # A settings read never writes: nothing is created yet.
    knob = read_runtime_config(data)["triage_repo_root"]
    assert knob["source"] == "default" and knob["available"] is True and knob["exists"] is False
    assert not (data / "backlog").exists()

    res = resolve_backlog_root(data)  # first use
    backlog = data / "backlog" / "docs" / "backlog"
    for folder in ("planned", "proposed", "completed"):
        assert (backlog / folder).is_dir()
    shipped = resources.files("abstractgateway") / "assets" / "backlog_skeleton"
    for name in ("overview.md", "template.md"):
        assert (backlog / name).read_text(encoding="utf-8") == (shipped / name).read_text(encoding="utf-8")
    assert "docs/backlog/template.md" in res["created"]
    assert "{ID}" in (backlog / "template.md").read_text(encoding="utf-8")

    # Never overwrites what the user changed.
    (backlog / "overview.md").write_text("mine", encoding="utf-8")
    again = resolve_backlog_root(data)
    assert "created" not in again
    assert (backlog / "overview.md").read_text(encoding="utf-8") == "mine"


def test_a_vanished_stored_folder_reports_the_reason(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import resolve_backlog_root, resolve_triage_repo_root, write_runtime_config

    data = tmp_path / "data"
    repo = _repo(tmp_path, "project")
    write_runtime_config(data, {"triage_repo_root": str(repo)}, actor="t")
    shutil.rmtree(repo)
    res = resolve_backlog_root(data)
    assert res["available"] is False
    assert res["reason"] == "the folder does not exist (set by the saved setting)"
    assert str(repo) not in res["reason"]  # the reason is path-free (shown to non-admins)
    assert resolve_triage_repo_root(data) is None


# ------------------------------------------------------------------ validation


def test_one_validation_for_every_door(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, validate_backlog_root, write_runtime_config

    data = tmp_path / "data"
    no_backlog = tmp_path / "plain"
    no_backlog.mkdir()
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")

    cases = {
        str(tmp_path / "missing"): "the folder does not exist",
        str(a_file): "it is a file, not a folder",
        str(no_backlog): "it has no docs/backlog folder",
    }
    for raw, why in cases.items():
        with pytest.raises(RuntimeConfigError) as exc:
            write_runtime_config(data, {"triage_repo_root": raw}, actor="t")
        assert why in str(exc.value)
        assert str(data / "backlog") in str(exc.value)  # names the one-step alternative
        with pytest.raises(RuntimeConfigError):
            validate_backlog_root(raw, data, what="--backlog-root")

    # The gateway's own folder is always accepted, and created.
    out = write_runtime_config(data, {"triage_repo_root": str(data / "backlog")}, actor="t")
    assert out["triage_repo_root"]["source"] == "stored"
    assert (data / "backlog" / "docs" / "backlog" / "template.md").is_file()

    # Switches are on/off; a typo refuses instead of storing "off".
    with pytest.raises(RuntimeConfigError, match="backlog_exec_runner is on or off"):
        write_runtime_config(data, {"backlog_exec_runner": "of"}, actor="t")
    with pytest.raises(RuntimeConfigError, match="process_manager is on or off"):
        write_runtime_config(data, {"process_manager": "maybe"}, actor="t")
    assert write_runtime_config(data, {"process_manager": "on"}, actor="t")["process_manager"]["value"] is True


# -------------------------------------------------------------------- routes


def test_fresh_gateway_serves_an_empty_backlog_not_a_setup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _serve_env(monkeypatch, tmp_path)
    with _client() as client:
        st = client.get("/api/gateway/backlog/status")
        assert st.status_code == 200, st.text
        body = st.json()
        assert body["available"] is True and body["source"] == "default" and body["is_default"] is True
        assert body["path"] == str((data / "backlog").resolve())
        for kind in ("planned", "proposed", "completed"):
            r = client.get(f"/api/gateway/backlog/{kind}")
            assert r.status_code == 200, r.text
            assert r.json()["items"] == []
        tpl = client.get("/api/gateway/backlog/template")
        assert tpl.status_code == 200, tpl.text
        assert tpl.json()["content"].startswith("# {ID}")
        # Creating the first item lands in the gateway's own folder.
        made = client.post("/api/gateway/backlog/create", json={"kind": "proposed", "package": "framework", "title": "First item"})
        assert made.status_code == 200, made.text
        assert (data / "backlog" / "docs" / "backlog" / "proposed" / made.json()["filename"]).is_file()


def test_backlog_routes_honour_the_stored_setting_and_explain_a_vanished_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_env(monkeypatch, tmp_path)
    repo = _repo(tmp_path, "project")
    (repo / "docs" / "backlog" / "planned" ).mkdir()
    (repo / "docs" / "backlog" / "planned" / "0001_x.md").write_text("# 0001 — X\n", encoding="utf-8")
    with _client() as client:
        w = client.post("/api/gateway/admin/runtime-config", json={"triage_repo_root": str(repo)})
        assert w.status_code == 200, w.text
        items = client.get("/api/gateway/backlog/planned").json()["items"]
        assert [i["filename"] for i in items] == ["0001_x.md"]  # the stored folder is READ (it was env-only before)

        shutil.rmtree(repo)
        r = client.get("/api/gateway/backlog/planned")
        assert r.status_code == 404
        detail = r.json()["detail"]
        assert detail.startswith("Backlog folder not available on this gateway: the folder does not exist (set by the saved setting)")
        assert str(repo) not in detail
        assert "ABSTRACTGATEWAY_" not in detail
        st = client.get("/api/gateway/backlog/status").json()
        assert st["available"] is False and st["path"] == str(repo.resolve())

        bad = client.post("/api/gateway/admin/runtime-config", json={"triage_repo_root": str(repo)})
        assert bad.status_code == 400
        assert "cannot be the backlog folder: the folder does not exist" in bad.json()["detail"]


# ----------------------------------------------------------------------- CLI


def _config_main(argv: list[str]) -> int:
    from abstractgateway.config_cli import main as config_main

    try:
        config_main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_config_get_set_unset_through_the_one_door(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    repo = _repo(tmp_path, "project")

    assert _config_main(["get", "triage_repo_root", "--json", "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "default"

    assert _config_main(["set", "triage_repo_root", str(repo), "--data-dir", str(data)]) == 0
    out = capsys.readouterr()
    assert f"triage_repo_root = {repo.resolve()}  [saved setting]" in out.out
    assert "saved in" in out.err

    assert _config_main(["set", "backlog_exec_runner", "on", "--data-dir", str(data)]) == 0
    assert "backlog_exec_runner = on  [saved setting]" in capsys.readouterr().out
    assert _config_main(["set", "process_manager", "on", "--data-dir", str(data)]) == 0
    capsys.readouterr()

    assert _config_main(["set", "triage_repo_root", str(tmp_path / "nowhere"), "--data-dir", str(data)]) == 2
    assert "refused: triage_repo_root" in capsys.readouterr().err

    assert _config_main(["get", "--data-dir", str(data)]) == 0
    listing = capsys.readouterr().out
    for line in ("triage_repo_root = ", "backlog_exec_runner = on  [saved setting]", "process_manager = on  [saved setting]"):
        assert line in listing

    assert _config_main(["unset", "backlog_exec_runner", "--data-dir", str(data)]) == 0
    assert "backlog_exec_runner = off  [default]" in capsys.readouterr().out

    from abstractgateway.runtime_config import read_runtime_config

    cfg = read_runtime_config(data)
    assert cfg["triage_repo_root"]["value"] == str(repo.resolve())
    assert cfg["backlog_exec_runner"]["source"] == "default"


def test_config_set_goes_through_the_running_gateway_when_one_serves_this_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from abstractgateway import config_cli

    data = tmp_path / "data"
    (data / "run").mkdir(parents=True)
    (data / "auth").mkdir(parents=True)
    (data / "auth" / "bootstrap-admin-token").write_text("tok", encoding="utf-8")
    record = {"pid": os.getpid(), "url": "http://127.0.0.1:18999", "host": "127.0.0.1", "port": 18999}
    (data / "run" / "gateway-serve.json").write_text(json.dumps(record), encoding="utf-8")
    assert config_cli._live_gateway(data) == {"url": "http://127.0.0.1:18999", "token": "tok"}

    calls: list = []

    def refuse(live, change):
        calls.append((live, change))
        return 409, {"detail": "runtime config store is unreadable; refusing to write over it"}

    # The running gateway's refusal is final, even for a value this process
    # would accept: the CLI never writes around the gateway's door.
    monkeypatch.setattr(config_cli, "_post_runtime_config", refuse)
    assert _config_main(["set", "backlog_exec_runner", "on", "--data-dir", str(data)]) == 2
    assert "refused: runtime config store is unreadable" in capsys.readouterr().err
    assert calls == [({"url": "http://127.0.0.1:18999", "token": "tok"}, {"backlog_exec_runner": "on"})]
    assert not (data / "config" / "runtime_config.json").exists()  # a refusal never lands

    def accept(live, change):
        from abstractgateway.runtime_config import write_runtime_config

        return 200, write_runtime_config(data, change, actor="person:admin")

    monkeypatch.setattr(config_cli, "_post_runtime_config", accept)
    assert _config_main(["set", "backlog_exec_runner", "on", "--data-dir", str(data)]) == 0
    assert "applied by the running gateway (http://127.0.0.1:18999)" in capsys.readouterr().err

    # A remote or dead record is never used (no guessed gateway gets written).
    record["pid"] = 2**22 + 4321
    (data / "run" / "gateway-serve.json").write_text(json.dumps(record), encoding="utf-8")
    assert config_cli._live_gateway(data) is None


def test_abstractgateway_config_forwards_get(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli

    monkeypatch.setattr(cli, "_reserve_gguf_metal", lambda: None)
    data = tmp_path / "data"
    cli.main(["config", "get", "backlog_exec_runner", "--data-dir", str(data)])
    assert "backlog_exec_runner = off  [default]" in capsys.readouterr().out


def test_serve_launch_flags_validate_record_and_never_touch_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli
    from abstractgateway.runtime_config import read_launch_settings, resolve_backlog_root, resolve_exec_runner

    data = tmp_path / "data"
    repo = _repo(tmp_path, "project")
    before = dict(os.environ)

    args = argparse.Namespace(backlog_root=str(repo), exec_runner="on")
    cli._apply_backlog_launch_flags(args, data)
    assert read_launch_settings(data) == {"triage_repo_root": str(repo.resolve()), "backlog_exec_runner": True}
    assert resolve_backlog_root(data)["source"] == "flag"
    assert resolve_exec_runner(data)["source"] == "flag"

    # No flags: the previous run's record goes away.
    cli._apply_backlog_launch_flags(argparse.Namespace(backlog_root=None, exec_runner=None), data)
    assert read_launch_settings(data) == {}

    with pytest.raises(SystemExit) as exc:
        cli._apply_backlog_launch_flags(argparse.Namespace(backlog_root=str(tmp_path / "nowhere"), exec_runner=None), data)
    assert "Refusing to start: --backlog-root" in str(exc.value)
    assert "cannot be the backlog folder: the folder does not exist" in str(exc.value)

    assert dict(os.environ) == before


def test_serve_parser_takes_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_reserve_gguf_metal", lambda: None)

    # The flags exist on `serve` (an invalid choice is refused by argparse).
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["serve", "--exec-runner", "maybe"])
    assert exc.value.code == 2


def test_backlog_exec_runner_cli_passes_the_folder_as_an_argument_not_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI used to WRITE os.environ["ABSTRACTGATEWAY_TRIAGE_REPO_ROOT"]
    in-process; the folder now flows as the runner's argument."""
    from abstractgateway import cli
    from abstractgateway.maintenance import backlog_exec_runner as ber
    from abstractgateway.runtime_config import write_runtime_config

    data = tmp_path / "data"
    stored = _repo(tmp_path, "stored")
    flagged = _repo(tmp_path, "flagged")
    write_runtime_config(data, {"triage_repo_root": str(stored)}, actor="t")

    seen: list = []

    class FakeRunner:
        def __init__(self, *, gateway_data_dir, cfg, repo_root=None):
            seen.append(repo_root)

        def start(self):
            pass

        def stop(self):
            pass

    class SetEvent:
        """An already-set stop event: the CLI's wait loop exits at once."""

        def is_set(self):
            return True

        def wait(self, _t=None):
            return True

        def set(self):
            pass

    monkeypatch.setattr(ber, "BacklogExecRunner", FakeRunner)
    monkeypatch.setattr(cli.threading, "Event", SetEvent)
    monkeypatch.setattr(cli.signal, "signal", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_reserve_gguf_metal", lambda: None)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    before = dict(os.environ)

    cli.main(["backlog-exec-runner", "--data-dir", str(data)])
    cli.main(["backlog-exec-runner", "--data-dir", str(data), "--repo-root", str(flagged)])
    assert seen == [stored.resolve(), flagged.resolve()]
    after = dict(os.environ)
    for name in _LEGACY_ENV:
        assert name not in after
    assert after == before


def test_the_exec_runner_resolves_the_folder_each_poll(tmp_path: Path) -> None:
    from abstractgateway.maintenance.backlog_exec_runner import _resolved_backlog_root
    from abstractgateway.runtime_config import write_runtime_config

    data = tmp_path / "data"
    assert _resolved_backlog_root(data) == (data / "backlog").resolve()
    repo = _repo(tmp_path, "project")
    write_runtime_config(data, {"triage_repo_root": str(repo)}, actor="t")
    assert _resolved_backlog_root(data) == repo.resolve()
    shutil.rmtree(repo)
    assert _resolved_backlog_root(data) is None


def test_console_continuum_settings_live_behind_the_card_gear() -> None:
    """Round 8: no "Advanced" disclosures on the Apps page. Continuum's settings
    open in the settings modal from the gear on its card; the apps.* settings
    from the gear in the Apps toolbar. No Save button: rows apply on blur /
    switch. No environment variable taught, no "Environment (legacy)" source."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    apps_tab = html[html.index('<div id="tab-apps" class="tab-panel">') : html.index('<div id="tab-network"')]
    assert "<details" not in apps_tab
    assert 'id="backlog-settings-root"' not in html and 'id="apps-settings-root"' not in html
    assert "Advanced: backlog settings" not in html and "Advanced: apps settings" not in html
    assert '<div id="app-settings-backdrop" class="af-modal-backdrop" hidden>' in html
    assert 'continuum: { title: "Continuum settings", mount: (el) => mountBacklogSettings(el)' in html
    assert 'if (action === "settings") { appSettingsModalOpen(id); return; }' in html
    assert 'data-app-action="settings"' in html and 'data-app-action="apps-settings"' in html
    start = html.index("const backlogSetStore = ")
    card = html[start : html.index("function unmountBacklogSettings", start)]
    assert "ABSTRACTGATEWAY_" not in card
    assert "Environment (legacy)" not in card and '"env"' not in card.split("triage_repo_root", 1)[0]
    assert "Save backlog settings" not in html and "Save apps settings" not in html


def test_console_continuum_settings_markup_rows_and_switches() -> None:
    """The modal body, rendered in node: the folder field, the two switches
    (role=switch, the effective state), the launch flag locks a switch, a
    non-admin sees no enabled control, and there is no Save button."""
    from test_gateway_console_offline import _console_script, _node, _slice_function

    source = _console_script()
    harness = f"""
const HTML_ESCAPES = {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
const backlogSetStore = {{ data: null, error: "", busy: null, rows: {{}}, draft: {{}}, el: null }};
const BACKLOG_SET_KEYS = ["triage_repo_root", "backlog_exec_runner", "process_manager"];
{_slice_function(source, "backlogRowNote")}
{_slice_function(source, "backlogSettingsMarkup")}
const base = {{
  triage_repo_root: {{ label: "Backlog folder", help: "h", source: "stored", value: "/repo", default_path: "/d/backlog", available: true }},
  backlog_exec_runner: {{ label: "Backlog exec runner", help: "runs", source: "default", value: false }},
  process_manager: {{ label: "Process manager", help: "services", source: "stored", value: true }},
}};
const out = {{}};
backlogSetStore.data = {{ writable: true, ...base }};
out.admin = backlogSettingsMarkup();
backlogSetStore.rows = {{ process_manager: {{ tone: "ok", text: "Saved" }} }};
out.saved = backlogSettingsMarkup();
backlogSetStore.rows = {{}};
backlogSetStore.data = {{ writable: true, ...base, backlog_exec_runner: {{ label: "Backlog exec runner", help: "runs", source: "flag", flag: "--exec-runner on|off", value: true }} }};
out.flag = backlogSettingsMarkup();
backlogSetStore.data = {{ writable: false, ...base }};
out.user = backlogSettingsMarkup();
console.log(JSON.stringify([out]));
"""
    out = _node(harness)[0]
    admin = out["admin"]
    assert "<details" not in admin and ">Save" not in admin
    assert 'id="backlog-set-triage_repo_root" data-backlog-input="triage_repo_root"' in admin and 'value="/repo"' in admin
    assert 'data-backlog-switch="backlog_exec_runner" aria-checked="false"' in admin
    assert 'data-backlog-switch="process_manager" aria-checked="true"' in admin
    assert admin.count('role="switch"') == 2
    assert "Use the gateway's own folder" in admin
    assert 'data-backlog-setting-saved="process_manager">Saved<' in out["saved"]
    flag = out["flag"]
    assert 'data-backlog-switch="backlog_exec_runner" aria-checked="true" aria-disabled="true"' in flag
    assert "Set for this run by the launch flag --exec-runner on|off" in flag
    user = out["user"]
    assert "Only an admin can change these." in user and " disabled" in user
    assert 'data-backlog-switch="process_manager" aria-checked="true" aria-disabled="true"' in user
    assert "Use the gateway" not in user


def test_the_launch_record_is_removed_by_its_own_process_only(tmp_path: Path) -> None:
    """SIGTERM never reaches the CLI's `finally`: the app's shutdown hook
    removes the record, but only the one THIS process wrote."""
    from abstractgateway.runtime_config import clear_launch_settings, read_launch_settings, record_launch_settings

    data = tmp_path / "data"
    record_launch_settings(data, {"backlog_exec_runner": True})
    clear_launch_settings(data, pid=os.getpid() + 1)
    assert read_launch_settings(data) == {"backlog_exec_runner": True}
    clear_launch_settings(data, pid=os.getpid())
    assert read_launch_settings(data) == {}
