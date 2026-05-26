from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def test_gateway_config_status_reports_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ABSTRACTCORE_SERVER_BASE_URL", "http://core.test/v1")
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
    assert "abstractcore-config" in text

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
