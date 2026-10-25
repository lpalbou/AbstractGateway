"""Gateway online update and automatic restart behavior."""
import pytest

def test_auto_restart_is_requested_only_after_success(monkeypatch):
    from abstractgateway import self_update, host_control
    self_update._reset_for_tests()
    monkeypatch.setattr(host_control, "control_capabilities", lambda: {"restart": True})
    calls = []
    monkeypatch.setattr(host_control, "request_restart", lambda **kw: calls.append(kw))
    monkeypatch.setattr(self_update, "_installed_version_fresh", lambda _: "0.2.0")
    info = self_update.InstallInfo(kind="pip", upgradable=True, reason=None, command=["pip", "install"], display_command="pip install", python="/py", prefix="/venv", version="0.1.0")
    self_update.start_update(info=info, runner=lambda *args: 0, auto_restart=True)
    self_update._job_thread.join(3)
    assert len(calls) == 1
    assert self_update.job_status()["state"] == "succeeded"
    self_update._reset_for_tests()
    calls.clear()
    self_update.start_update(info=info, runner=lambda *args: 1, auto_restart=True)
    self_update._job_thread.join(3)
    assert not calls
    assert self_update.job_status()["state"] == "failed"
    self_update._reset_for_tests()


def test_auto_restart_unavailable_refuses_before_install(monkeypatch):
    from abstractgateway import self_update, host_control
    monkeypatch.setattr(host_control, "control_capabilities", lambda: {"restart": False, "reason": "Development reload mode"})
    with pytest.raises(self_update.UpdateNotPossible, match="Development reload"):
        self_update.start_update(runner=lambda *args: pytest.fail("must not install"), auto_restart=True)


def test_restart_failure_keeps_successful_install_visible(monkeypatch):
    from abstractgateway import self_update, host_control
    self_update._reset_for_tests()
    monkeypatch.setattr(host_control, "control_capabilities", lambda: {"restart": True})
    def fail_restart(**kwargs):
        raise RuntimeError("Restart unavailable")
    monkeypatch.setattr(host_control, "request_restart", fail_restart)
    monkeypatch.setattr(self_update, "_installed_version_fresh", lambda _: "0.2.0")
    info = self_update.InstallInfo(kind="pip", upgradable=True, reason=None, command=["pip", "install"], display_command="pip install", python="/py", prefix="/venv", version="0.1.0")
    self_update.start_update(info=info, runner=lambda *args: 0, auto_restart=True)
    self_update._job_thread.join(3)
    assert self_update.job_status()["state"] == "succeeded"
    assert self_update.job_status()["restart_error"] == "Restart unavailable"
    assert self_update.job_status()["restart_recommended"]
    self_update._reset_for_tests()
