"""Tray "Check for Updates…" renders the gateway's own update view (2026-09-28).

The tray, the web console and the terminal console show ONE rendering
(`self_update.update_view`): for an AbstractFramework installer install the confirmation
names the installer (the one-line install's URL, commit, sha256, the command), the start
sends back the sha256 it showed, and the result is reported honestly: installed (restart
offered), already up to date (no restart), or failed with the job's own reason and log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from abstractgateway.tray import app as tray_app
from abstractgateway.tray.client import Result

pytestmark = pytest.mark.basic

SHA = "ab" * 32
ACTION = {
    "label": "Update to AbstractFramework 0.6.2",
    "confirm": "AbstractFramework 0.6.2 (gateway 0.7.2) is available ... Update runs the AbstractFramework installer, the script the one-line install runs: https://raw.githubusercontent.com/lpalbou/AbstractFramework/main/scripts/install.sh (commit 0123456789ab, sha256 abab…)",
    "command": "/bin/sh install.sh --yes --no-start --no-open --no-modify-path --data-dir /d",
    "source": "https://raw.githubusercontent.com/lpalbou/AbstractFramework/main/scripts/install.sh",
    "installer_sha256": SHA,
}


def _overview(status: str, *, job: Optional[Dict[str, Any]] = None, action: Optional[Dict[str, Any]] = None, line: str = "AbstractFramework 0.6.1 · gateway 0.7.1 · …", hint: str = "", offer: Optional[str] = "AbstractFramework 0.6.2") -> Dict[str, Any]:
    return {
        "ok": True,
        "current": "0.7.1",
        "install": {"kind": "installer", "upgradable": True},
        "check": {"update_available": status == "available"},
        "job": job or {"state": "idle"},
        "update": {"status": status, "line": line, "hint": hint, "offer": offer, "action": action, "checked_at": None},
    }


class _Client:
    def __init__(self, check: Dict[str, Any], states: List[Dict[str, Any]]) -> None:
        self.check = check
        self.states = list(states)
        self.started: List[Optional[str]] = []
        self.restarted = 0

    def check_update(self) -> Result:
        return Result(True, 200, self.check, "")

    def start_update(self, installer_sha256: Optional[str] = None) -> Result:
        self.started.append(installer_sha256)
        return Result(True, 200, self.states[0], "")

    def update_state(self) -> Result:
        return Result(True, 200, self.states.pop(0) if len(self.states) > 1 else self.states[0], "")

    def restart(self, reason: Optional[str] = None) -> Result:
        self.restarted += 1
        return Result(True, 200, {}, "")


@pytest.fixture
def tray(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = tray_app.TrayApp({"base_url": "http://127.0.0.1:8080", "token": "", "data_dir": str(tmp_path)})
    shown: List[Tuple[str, str]] = []
    confirms: List[Tuple[str, str]] = []
    answers: List[Optional[bool]] = []
    monkeypatch.setattr(app, "_bg", lambda fn, name="": fn())
    monkeypatch.setattr(app, "_force_menu_rebuild", lambda: None)
    monkeypatch.setattr(app, "_info", lambda title, body, style="informational": shown.append((title, body)))
    monkeypatch.setattr(app.sampler, "set_override", lambda value: None)
    monkeypatch.setattr(tray_app, "UPDATE_POLL_SECONDS", 0.0, raising=False)

    def confirm(title: str, body: str, **kw: Any) -> Optional[bool]:
        confirms.append((title, body))
        return answers.pop(0) if answers else False

    monkeypatch.setattr(tray_app.dialogs, "confirm", confirm)
    return app, shown, confirms, answers


def test_the_offer_is_the_gateways_confirmation_and_the_start_sends_its_sha(tray) -> None:
    app, shown, confirms, answers = tray
    done = _overview("installed", job={"state": "succeeded", "message": "AbstractFramework 0.6.2 is installed (abstractgateway 0.7.1 -> 0.7.2)"})
    app.client = _Client(_overview("available", action=ACTION), [_overview("running", job={"state": "running"}), done])
    answers.extend([True, False])  # Update Now, then Later for the restart
    app.check_or_apply_update()
    assert confirms[0] == ("Update available", ACTION["confirm"])
    assert app.client.started == [SHA]
    assert confirms[1][0] == "Update installed" and "AbstractFramework 0.6.2 is installed (abstractgateway 0.7.1 -> 0.7.2)" in confirms[1][1]
    assert app._update_phase == "installed" and app.client.restarted == 0
    # The menu label names what the update installs.
    assert app._update_latest == "AbstractFramework 0.6.2"


def test_already_up_to_date_is_not_reported_as_a_failure(tray) -> None:
    app, shown, confirms, answers = tray
    same = _overview("no_change", job={"state": "succeeded_no_change", "message": "already up to date: AbstractFramework 0.6.2; the installer changed nothing"})
    app.client = _Client(_overview("available", action=ACTION), [same])
    answers.append(True)
    app.check_or_apply_update()
    assert shown == [("Already up to date", "already up to date: AbstractFramework 0.6.2; the installer changed nothing")]
    assert app._update_phase == "idle"


def test_a_failed_update_shows_the_jobs_reason_and_the_last_log_lines(tray) -> None:
    app, shown, confirms, answers = tray
    failed = _overview("failed", job={"state": "failed", "error": "the AbstractFramework installer exited with code 1: …", "log_tail": ["[3] AbstractGateway", "ERROR: no wheel for webrtcvad"]})
    app.client = _Client(_overview("available", action=ACTION), [failed])
    answers.append(True)
    app.check_or_apply_update()
    title, body = shown[-1]
    assert title == "The update didn't finish" and app._update_phase == "failed"
    assert "installer exited with code 1" in body and "ERROR: no wheel for webrtcvad" in body


def test_up_to_date_and_not_possible_say_the_gateways_words(tray) -> None:
    app, shown, confirms, answers = tray
    app.client = _Client(_overview("up_to_date", line="AbstractFramework 0.6.2 · gateway 0.7.2 · up to date", offer=None), [])
    app.check_or_apply_update()
    assert shown[-1] == ("You're up to date", "AbstractFramework 0.6.2 · gateway 0.7.2 · up to date") and not confirms
    win_hint = "Windows keeps the running gateway's files locked … — run: powershell -ExecutionPolicy ByPass -c \"irm …/install.ps1 | iex\""
    app.client = _Client(_overview("not_possible", hint=win_hint), [])
    app.check_or_apply_update()
    assert shown[-1] == ("AbstractFramework 0.6.2 is available, but not from here", win_hint)
    assert app._update_phase == "not_possible" and not confirms
