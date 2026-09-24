from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _core_store_path() -> Path:
    """THE AbstractCore store for this test process.

    ONE STORE (operator ruling 2026-08-01): the Gateway has no base of its own,
    so a Gateway-wide default is a row in AbstractCore's config file -- the very
    file `abstractcore config defaults` reads. The suite points
    `ABSTRACTCORE_CONFIG_FILE` at a throwaway path (tests/conftest.py).
    """
    return Path(os.environ["ABSTRACTCORE_CONFIG_FILE"])


def _empty_core_store(path: Path) -> Path:
    """An EXISTING AbstractCore store that configures nothing.

    Since the operator ruling of 2026-08-01 an ABSENT store means a truly fresh
    install and AbstractCore seeds it with the recommended stack, so "no route
    is configured here" has to be said by materializing the file -- the same
    pattern AbstractCore's own `tests/config` use.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


def test_gateway_config_status_reports_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ABSTRACTCORE_SERVER_BASE_URL", "http://core.test/v1")
    # "core.test" stands for a remote AbstractCore server that is not there.
    # Answer "unavailable" here instead of resolving it on the real network
    # (network guard finding, 2026-09-24).
    import abstractgateway.core_config as _core_config

    def _unreachable_core_server(method, path, body=None):
        raise RuntimeError("AbstractCore config route unavailable: core.test is a stand-in")

    monkeypatch.setattr(_core_config, "_core_server_json", _unreachable_core_server)
    monkeypatch.setenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN", "core-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", "sqlite")

    from abstractgateway.config_cli import main

    main(["status", "--json"])
    out = json.loads(capsys.readouterr().out)

    assert out["gateway"]["auth_configured"] is True
    assert out["core_server"]["base_url"] == "http://core.test/v1"
    assert out["core_server"]["auth_configured"] is True
    assert out["memory"]["backend"] == "sqlite"
    assert "Gateway auth is separate" in out["core_server"]["note"]


def test_gateway_config_init_writes_private_env_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from abstractgateway.config_cli import main

    env_file = tmp_path / "gateway.env"
    main(
        [
            "init",
            "--env-file",
            str(env_file),
            "--auth-token",
            "secret-token",
            "--data-dir",
            str(tmp_path / "runtime"),
            "--flows-dir",
            str(tmp_path / "flows"),
            "--store-backend",
            "sqlite",
            "--memory-backend",
            "sqlite",
            "--core-server-url",
            "http://core.test/v1",
        ]
    )

    text = env_file.read_text(encoding="utf-8")
    assert "ABSTRACTGATEWAY_AUTH_TOKEN=secret-token" in text
    assert "ABSTRACTGATEWAY_STORE_BACKEND=sqlite" in text
    assert "ABSTRACTGATEWAY_MEMORY_STORE_BACKEND=sqlite" in text
    assert "ABSTRACTCORE_SERVER_BASE_URL=http://core.test/v1" in text
    assert "ABSTRACTGATEWAY_PROVIDER" not in text
    assert "ABSTRACTGATEWAY_MODEL" not in text
    assert "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL" not in text
    assert "Gateway Console" in text

    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode & stat.S_IRWXG == 0
    assert mode & stat.S_IRWXO == 0
    assert "Wrote" in capsys.readouterr().out


def test_gateway_config_init_refuses_to_overwrite(tmp_path: Path) -> None:
    from abstractgateway.config_cli import main

    env_file = tmp_path / "gateway.env"
    env_file.write_text("EXISTING=1\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        main(["init", "--env-file", str(env_file)])


def test_gateway_config_defaults_set_list_and_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    from abstractgateway.config_cli import main

    main(
        [
            "set-default",
            "output.text",
            "--provider",
            "lmstudio",
            "--model",
            "qwen/qwen3.6-35b-a3b",
            "--base-url",
            "http://127.0.0.1:1234/v1",
            "--option",
            "temperature=0.2",
        ]
    )
    assert "Set execution-host capability default: output.text" in capsys.readouterr().out

    main(["defaults", "--json"])
    out = json.loads(capsys.readouterr().out)
    route = next(item for item in out["routes"] if item["key"] == "output.text")
    assert route["provider"] == "lmstudio"
    assert route["model"] == "qwen/qwen3.6-35b-a3b"
    assert route["base_url"] == "http://127.0.0.1:1234/v1"
    assert route["options"] == {"temperature": 0.2}

    main(["clear-default", "output.text"])
    assert "Cleared execution-host capability default: output.text" in capsys.readouterr().out

    main(["defaults", "--json"])
    out = json.loads(capsys.readouterr().out)
    route = next(item for item in out["routes"] if item["key"] == "output.text")
    assert route["configured"] is False


def test_gateway_config_defaults_write_gateway_and_user_scoped_core_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    from abstractgateway.config_cli import main
    from abstractgateway.users import GatewayUserRegistry

    main(["set-default", "output.text", "--provider", "openrouter", "--model", "gateway-model"])
    assert "Set execution-host capability default: output.text" in capsys.readouterr().out
    # A Gateway-wide default is a write to THE Core store, not to a second base
    # under the data dir: that store is retired (operator ruling 2026-08-01).
    core_store = _core_store_path()
    assert core_store.exists()
    assert not (tmp_path / "runtime" / "config" / "abstractcore.json").exists()
    assert not (tmp_path / "runtime" / "config" / "capability_defaults.json").exists()

    _record, _token = GatewayUserRegistry().create_user(user_id="alice", roles=["user"], runtime_id="alice")
    main(
        [
            "set-default",
            "output.text",
            "--scope",
            "user",
            "--user",
            "alice",
            "--provider",
            "anthropic",
            "--model",
            "alice-model",
        ]
    )
    assert "Set execution-host capability default: output.text" in capsys.readouterr().out
    alice_config = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime" / "config" / "abstractcore.json"
    assert alice_config.exists()

    main(["defaults", "--scope", "user", "--user", "alice", "--json"])
    alice_payload = json.loads(capsys.readouterr().out)
    alice_text = next(item for item in alice_payload["routes"] if item["key"] == "output.text")
    assert alice_text["provider"] == "anthropic"
    assert alice_text["model"] == "alice-model"
    assert alice_text["source"] == "abstractcore.runtime"

    _record, _token = GatewayUserRegistry().create_user(user_id="bob", roles=["user"], runtime_id="bob")
    main(["defaults", "--scope", "user", "--user", "bob", "--json"])
    bob_payload = json.loads(capsys.readouterr().out)
    bob_text = next(item for item in bob_payload["routes"] if item["key"] == "output.text")
    assert bob_text["provider"] == "openrouter"
    assert bob_text["model"] == "gateway-model"
    # Bob has no overlay, so his row IS the Core row, and says so.
    assert bob_text["source"] == "abstractcore.capability_defaults"


def test_gateway_config_defaults_ignore_legacy_capability_defaults_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    legacy = tmp_path / "runtime" / "config" / "capability_defaults.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({"version": 1, "routes": {"output.text": {"provider": "legacy-provider", "model": "legacy-model"}}}),
        encoding="utf-8",
    )
    # The live store exists and carries nothing, so an unconfigured route can
    # only mean the legacy overlay was ignored.
    _empty_core_store(_core_store_path())

    from abstractgateway.config_cli import main

    main(["defaults", "--json"])
    payload = json.loads(capsys.readouterr().out)
    route = next(item for item in payload["routes"] if item["key"] == "output.text")
    assert route["configured"] is False
    assert route.get("provider") != "legacy-provider"

    main(["set-default", "output.text", "--provider", "openrouter", "--model", "gateway-model"])
    assert "Set execution-host capability default: output.text" in capsys.readouterr().out
    data = json.loads(_core_store_path().read_text(encoding="utf-8"))
    assert data["capability_defaults"]["routes"]["input.text"]["model"] == "gateway-model"
    assert "output.text" not in data["capability_defaults"]["routes"]
    assert json.loads(legacy.read_text(encoding="utf-8"))["routes"]["output.text"]["model"] == "legacy-model"


def test_gateway_config_defaults_ignore_removed_overlay_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    old_overlay = tmp_path / "runtime" / "config" / "capability_defaults.json"
    old_overlay.parent.mkdir(parents=True)
    old_overlay.write_text(
        json.dumps({"version": 1, "routes": {"output.text": {"provider": "legacy", "model": "legacy-model"}}}),
        encoding="utf-8",
    )
    _empty_core_store(_core_store_path())

    from abstractgateway.config_cli import main

    main(["defaults", "--json"])
    payload = json.loads(capsys.readouterr().out)
    route = next(item for item in payload["routes"] if item["key"] == "output.text")
    assert route["configured"] is False
    assert route.get("provider") != "legacy"
    assert route.get("model") != "legacy-model"


def test_gateway_config_bootstrap_admin_creates_user_and_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))

    from abstractgateway.config_cli import main
    from abstractgateway.users import GatewayUserRegistry

    token_file = tmp_path / "runtime" / "auth" / "bootstrap-admin-token"
    main(["bootstrap-admin", "--json", "--print-token"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["user"]["tenant_id"] == "default"
    assert payload["user"]["user_id"] == "admin"
    assert payload["user"]["runtime_id"] == "default"
    assert payload["user"]["roles"] == ["admin", "user"]
    assert payload["token"].startswith("agw_")
    assert token_file.read_text(encoding="utf-8").strip() == payload["token"]
    assert GatewayUserRegistry().authenticate(payload["token"]).user_id == "admin"  # type: ignore[union-attr]

    main(["bootstrap-admin", "--json", "--print-token"])
    second = json.loads(capsys.readouterr().out)
    assert second["changed"] is False
    assert second["token"] == payload["token"]
