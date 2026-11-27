"""The Windows desktop icon: a double-click starts the gateway unless it runs, then opens its console."""

from __future__ import annotations

import webbrowser
from pathlib import Path

import pytest

from abstractgateway import desktop_shortcut, os_service

pytestmark = pytest.mark.basic


@pytest.mark.parametrize("running", [True, False])
def test_double_click_starts_the_gateway_only_when_it_is_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: bool) -> None:
    started: list = []
    opened: list = []
    answers = iter([running, True])  # the first probe, then the one after the start
    monkeypatch.setattr(desktop_shortcut, "_answers", lambda url: next(answers))
    monkeypatch.setattr(desktop_shortcut.time, "sleep", lambda s: None)
    monkeypatch.setattr(os_service, "_start_detached", lambda argv: started.append(list(argv)))
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    assert desktop_shortcut.start_and_open(tmp_path) is None

    assert len(started) == (0 if running else 1)
    if started:
        # The login item's launcher (log file, tray), plain `serve`: the Network setting binds it.
        assert started[0][1:4] == ["-m", "abstractgateway.os_service", "launch"]
        assert started[0][-1] == "serve" and str(tmp_path) in started[0]
    # No onboarding record in this data dir, so not the setup page: the tray's link, on the default port.
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:8080/console")


def test_a_gateway_that_does_not_come_up_is_reported_with_its_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_shortcut, "_answers", lambda url: False)
    monkeypatch.setattr(desktop_shortcut.time, "sleep", lambda s: None)
    monkeypatch.setattr(desktop_shortcut, "START_WAIT_S", 0.0)
    monkeypatch.setattr(os_service, "_start_detached", lambda argv: None)
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("opened a console that is not there"))

    problem = desktop_shortcut.start_and_open(tmp_path)

    assert problem and "did not start" in problem and "gateway.log" in problem


def test_the_shortcut_runs_without_a_console_window(tmp_path: Path) -> None:
    argv = desktop_shortcut.launch_argv(tmp_path)
    assert Path(argv[0]).name.lower() == "pythonw.exe"
    assert argv[1:] == ["-m", "abstractgateway.desktop_shortcut", "--data-dir", str(tmp_path)]
    assert desktop_shortcut.ICON.is_file()
