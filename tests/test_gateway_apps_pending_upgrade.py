"""Boot applies the installer's apps-upgrade.pending (root installers, 0.9.1).

The installer writes `<data dir>/apps-upgrade.pending` ("ID VERSION" lines) when it upgrades with no
gateway running; the next boot brings every INSTALLED app named there to that version before the
apps start, leaves uninstalled apps alone, removes the file, and reports a failed update.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from abstractgateway import apps_manager as am


class _Offline:
    def __call__(self, *a: Any, **k: Any) -> Any:
        raise OSError("offline")


def _manager(tmp_path: Path, installed: Dict[str, str], fail: str = "") -> tuple:
    m = am.AppsManager(tmp_path / "runtime", urlopen=_Offline(), install_allowed=lambda: True)
    calls: List[tuple] = []
    m.installed_version = lambda app_id: installed.get(app_id)  # type: ignore[method-assign]

    def start_install(app_id: str, **kw: Any) -> tuple:
        calls.append((app_id, kw.get("version"), kw.get("update")))
        if app_id == fail:
            return SimpleNamespace(state="failed", message="npm failed", error={"message": "npm failed"}), True
        installed[app_id] = kw["version"]
        return SimpleNamespace(state="succeeded", message="ok", error=None), True

    m.start_install = start_install  # type: ignore[method-assign]
    return m, calls


def test_boot_applies_the_pending_marker(tmp_path: Path) -> None:
    m, calls = _manager(tmp_path, {"code": "0.9.0", "flow": "0.7.0"})
    marker = m.data_dir / am.APPS_UPGRADE_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("flow 0.7.0\ncode 0.10.0\nobserver 0.6.0\nbogus 1.0\n", encoding="utf-8")
    out = m.autostart()
    assert calls == [("code", "0.10.0", True)]
    assert {"app_id": "code", "ok": True, "upgrade": "0.9.0 -> 0.10.0", "message": "ok"} in out
    assert not marker.exists()
    assert m.apply_pending_upgrades() == []


def test_a_failed_pending_upgrade_is_reported(tmp_path: Path) -> None:
    m, calls = _manager(tmp_path, {"entity": "0.5.2"}, fail="entity")
    marker = m.data_dir / am.APPS_UPGRADE_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("entity 0.6.0\n", encoding="utf-8")
    out = m.apply_pending_upgrades()
    assert out and out[0]["ok"] is False and "npm failed" in out[0]["message"]
