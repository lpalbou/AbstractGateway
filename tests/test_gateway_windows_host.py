"""Windows host behavior: workspace file serving, the built-in deny list, service status and
console output."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from abstractgateway import os_service, workspace_browse as wb


@pytest.mark.skipif(os.name != "nt", reason="GetFinalPathNameByHandleW")
def test_a_workspace_file_opens_on_windows_and_an_outside_file_is_refused(tmp_path) -> None:
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    fd = wb._open_verified(root / "sub" / "a.txt", root)
    try:
        assert Path(wb._fd_path(fd)).resolve() == (root / "sub" / "a.txt").resolve()
        assert os.read(fd, 5) == b"hello"
    finally:
        os.close(fd)
    with pytest.raises(wb.WorkspacePathError):
        wb._open_verified(tmp_path / "outside.txt", root)


@pytest.mark.skipif(os.name != "nt", reason="NTFS directory junction")
def test_a_junction_out_of_the_workspace_is_refused_on_windows(tmp_path) -> None:
    import subprocess

    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    link = root / "j"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path)], check=True, capture_output=True)
    try:
        with pytest.raises(wb.WorkspacePathError):
            wb._open_verified(link / "outside.txt", root)
    finally:
        os.rmdir(link)


def test_windows_credential_stores_are_in_the_builtin_deny_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wb, "_windows_host", lambda: True)
    paths = {str(p).replace("\\", "/").lower() for p in wb.builtin_deny_paths(tmp_path / "data", home=tmp_path)}
    for rel in wb.BUILTIN_DENY_HOME_RELPATHS_WINDOWS:
        assert any(p.endswith(rel.lower()) for p in paths), rel


def test_posix_hosts_do_not_list_windows_appdata(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wb, "_windows_host", lambda: False)
    paths = {str(p).replace("\\", "/").lower() for p in wb.builtin_deny_paths(tmp_path / "data", home=tmp_path)}
    for rel in wb.BUILTIN_DENY_HOME_RELPATHS_WINDOWS:
        assert not any(p.endswith(rel.lower()) for p in paths), rel


class _EmptyRegistry:
    def get(self, key, value):
        return None


def test_windows_service_status_without_a_registration_names_the_run_value(tmp_path) -> None:
    st = os_service.service_status(platform="windows", home=tmp_path, data_dir=tmp_path / "data", registry=_EmptyRegistry())
    assert st["installed"] is False
    assert st["unit_path"] == os_service.windows_run_display()


def test_safe_stdio_replaces_what_a_cp1252_stream_cannot_encode(monkeypatch) -> None:
    from abstractgateway.stdio_safety import safe_stdio

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", errors="strict"))
    safe_stdio()
    print("network → localhost ✓", file=sys.stdout)
    sys.stdout.flush()
    assert raw.getvalue().decode("cp1252").strip() == "network ? localhost ?"


@pytest.mark.parametrize("module", ["abstractgateway/cli.py", "abstractgateway/config_cli.py"])
def test_both_commands_make_stdio_safe_first(module) -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / module).read_text(encoding="utf-8")
    body = src[src.index("\ndef main("):].split("\ndef ", 2)[1]
    assert body.index("safe_stdio()") < 200


@pytest.mark.parametrize("platform, area", [("darwin", "menu bar"), ("win32", "notification area of the taskbar"), ("linux", "system tray")])
def test_desktop_app_icon_location_follows_the_os(monkeypatch, platform, area) -> None:
    from abstractgateway import apps_manager

    monkeypatch.setattr(apps_manager.sys, "platform", platform)
    assert apps_manager._tray_area() == area


_CHILD_FLAGS = r"""
import json, subprocess, sys
seen = []
def record(self, *args, **kwargs):
    seen.append(int(kwargs.get("creationflags", 0) or 0))
    raise RuntimeError("recorded")
subprocess.Popen.__init__ = record
from abstractgateway.stdio_safety import hide_child_consoles
installed = hide_child_consoles()
for flags in (0, subprocess.CREATE_NEW_CONSOLE):
    try:
        subprocess.Popen(["nvidia-smi"], creationflags=flags)
    except RuntimeError:
        pass
try:
    subprocess.run(["nvidia-smi"])
except RuntimeError:
    pass
print(json.dumps({"installed": installed, "flags": seen}))
"""


def _child_flags(creationflags: int) -> dict:
    import json
    import subprocess

    # The base interpreter itself: a venv's python.exe is a launcher with a console of its own.
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    exe = getattr(sys, "_base_executable", None) or sys.executable
    out = subprocess.run(
        [exe, "-c", _CHILD_FLAGS], capture_output=True, text=True, timeout=120, creationflags=creationflags, env=env
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(os.name != "nt", reason="Windows console windows")
def test_without_a_console_every_child_process_starts_without_a_window() -> None:
    # The login entry runs under pythonw.exe: no console, so each console program the gateway
    # starts (nvidia-smi for the GPU meter every few seconds) would flash its own window.
    import subprocess

    res = _child_flags(subprocess.DETACHED_PROCESS)
    no_window, new_console = subprocess.CREATE_NO_WINDOW, subprocess.CREATE_NEW_CONSOLE
    assert res["installed"] is True
    assert res["flags"][0] & no_window
    assert res["flags"][1] == new_console  # a caller that asks for its own console keeps it
    assert res["flags"][2] & no_window  # subprocess.run goes through Popen too


@pytest.mark.skipif(os.name != "nt", reason="Windows console windows")
def test_with_a_console_child_processes_share_it_unchanged() -> None:
    import subprocess

    res = _child_flags(subprocess.CREATE_NO_WINDOW)  # a console without a window is still a console
    assert res == {"installed": False, "flags": [0, subprocess.CREATE_NEW_CONSOLE, 0]}


@pytest.mark.skipif(os.name == "nt", reason="the POSIX branch")
def test_hide_child_consoles_does_nothing_off_windows() -> None:
    from abstractgateway.stdio_safety import hide_child_consoles

    assert hide_child_consoles() is False


@pytest.mark.parametrize("module", ["abstractgateway/cli.py", "abstractgateway/tray/__main__.py"])
def test_the_gateway_and_the_tray_hide_child_consoles(module) -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / module).read_text(encoding="utf-8")
    assert "hide_child_consoles()" in src[src.index("\ndef main("):]
