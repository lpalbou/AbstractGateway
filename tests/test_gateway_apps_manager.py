"""Browser apps manager: registry resolution + integrity, jobs, supervision,
ports, Node wheel selection, sign-in handover (routes), CLI.

No network and no Node.js: the registry is a fake `urlopen`, and the "node"
that runs an app is a tiny shell shim that runs a Python HTTP server, so the
supervision tests exercise the real Popen/readiness/restart code paths.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import stat
import sys
import tarfile
import time
import urllib.error
import zipfile
from pathlib import Path
from typing import Dict

import pytest

from abstractgateway import apps_manager as am

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeNet:
    def __init__(self) -> None:
        self.routes: Dict[str, bytes] = {}
        self.calls: list = []
        self.offline = False

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        if self.offline:
            raise urllib.error.URLError("nodename nor servname provided")
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return _Resp(self.routes[url])


_SERVER_PY = r'''
import http.server, os, sys, pathlib
mode = (pathlib.Path(__file__).parent / "mode").read_text().strip() if (pathlib.Path(__file__).parent / "mode").exists() else "serve"
if mode == "exit":
    print("boom: refusing to start", flush=True)
    sys.exit(3)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("ok " + os.environ.get("ABSTRACTGATEWAY_URL", "") + " token=" + os.environ.get("ABSTRACTGATEWAY_AUTH_TOKEN", "-")).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
print("listening", os.environ["PORT"], flush=True)
http.server.HTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ["PORT"])), H).serve_forever()
'''


def _make_tgz(name: str, version: str, *, deps: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        files = {
            "package/package.json": json.dumps({"name": name, "version": version, "bin": {"app": "bin/cli.js"}, "dependencies": deps or {}}).encode(),
            "package/bin/cli.js": _SERVER_PY.encode(),
            "package/dist/index.html": b"<!doctype html><title>x</title>",
        }
        for path, data in files.items():
            ti = tarfile.TarInfo(path)
            ti.size = len(data)
            ti.mode = 0o755 if path.endswith("cli.js") else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def _registry_doc(spec: am.AppSpec, versions: dict, latest: str) -> dict:
    return {
        "name": spec.package,
        "dist-tags": {"latest": latest},
        "versions": {
            v: {
                "name": spec.package,
                "version": v,
                "dist": {"tarball": f"https://registry.npmjs.org/{spec.package}/-/{spec.short_name}-{v}.tgz", "integrity": integ},
            }
            for v, integ in versions.items()
        },
    }


@pytest.fixture()
def fake_node(tmp_path: Path) -> Path:
    shim = tmp_path / "fake-node"
    shim.write_text(f'#!/bin/sh\nif [ "$1" = "-r" ]; then shift 2; fi\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    return shim


@pytest.fixture()
def manager(tmp_path: Path, fake_node: Path, monkeypatch: pytest.MonkeyPatch):
    net = FakeNet()
    m = am.AppsManager(tmp_path / "data", urlopen=net, install_allowed=lambda: True)
    m.net = net  # type: ignore[attr-defined]
    node = {"available": True, "version": "24.0.0", "source": "system", "path": str(fake_node), "npm": False, "npm_command": None, "problems": [], "install_available": False, "message": "fake"}
    monkeypatch.setattr(m, "node_status", lambda refresh=False: dict(node))
    monkeypatch.setenv(am.ENV_PORTS, "")
    monkeypatch.setattr(am, "RESTART_BACKOFF_S", 0.01)
    yield m
    m.stop_all()


def _publish(m: am.AppsManager, spec: am.AppSpec, version: str, *, tamper: bool = False) -> bytes:
    data = _make_tgz(spec.package, version)
    integ = _sri(data)
    if tamper:
        data = data + b"x"
    base = m.registry_url + "/" + spec.package.replace("/", "%2F")
    m.net.routes[base] = json.dumps(_registry_doc(spec, {version: integ}, version)).encode()  # type: ignore[attr-defined]
    m.net.routes[f"https://registry.npmjs.org/{spec.package}/-/{spec.short_name}-{version}.tgz"] = data  # type: ignore[attr-defined]
    return data


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Registry resolution + integrity
# ---------------------------------------------------------------------------


def test_resolve_from_metadata_latest_and_exact() -> None:
    spec = am.APP_BY_ID["code"]
    doc = _registry_doc(spec, {"0.4.1": "sha512-AAAA", "0.4.2": "sha512-BBBB"}, "0.4.2")
    doc["versions"]["0.4.2"]["dependencies"] = {"react": "^18"}
    info = am.resolve_from_metadata(doc, spec)
    assert info["version"] == "0.4.2"
    assert info["tarball"].endswith("/@abstractframework/code/-/code-0.4.2.tgz")
    assert info["dependencies"] == ["react"]
    assert am.resolve_from_metadata(doc, spec, "0.4.1")["integrity"] == "sha512-AAAA"
    with pytest.raises(am.AppsError, match="no version '9.9.9'"):
        am.resolve_from_metadata(doc, spec, "9.9.9")


def test_resolve_rejects_foreign_tarball_url() -> None:
    spec = am.APP_BY_ID["code"]
    doc = _registry_doc(spec, {"1.0.0": "sha512-AAAA"}, "1.0.0")
    doc["versions"]["1.0.0"]["dist"]["tarball"] = "https://evil.example/code-1.0.0.tgz"
    with pytest.raises(am.AppsError, match="unexpected tarball URL"):
        am.resolve_from_metadata(doc, spec)


def test_verify_sri() -> None:
    data = b"hello"
    digest = hashlib.sha512(data).digest()
    assert am.verify_sri(digest, _sri(data))
    assert am.verify_sri(digest, "sha1-xyz " + _sri(data))
    assert not am.verify_sri(digest, _sri(b"other"))
    assert not am.verify_sri(digest, "sha1-" + base64.b64encode(hashlib.sha1(data).digest()).decode())
    assert not am.verify_sri(digest, "")


def test_version_ordering() -> None:
    assert am.version_newer("0.4.10", "0.4.9")
    assert am.version_newer("1.0.0", "1.0.0-rc.1")
    assert not am.version_newer("0.4.2", "0.4.2")
    assert not am.version_newer("garbage", "0.1.0")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_job_state_machine(tmp_path: Path) -> None:
    job = am.Job(kind="k", target="t", app_id=None, log_dir=tmp_path, title="T")
    assert job.state == "queued"
    job.transition("running")
    job.transition("succeeded")
    assert job.terminal and job.finished_at is not None
    with pytest.raises(am.InvalidTransition):
        job.transition("running")
    job2 = am.Job(kind="k", target="t", app_id=None, log_dir=tmp_path, title="T")
    with pytest.raises(am.InvalidTransition):
        job2.transition("succeeded")  # queued -> succeeded skips running


def test_job_registry_dedupes_active_target_and_reports_failure(tmp_path: Path) -> None:
    import threading

    reg = am.JobRegistry(tmp_path)
    gate = threading.Event()

    def slow(job):
        gate.wait(5)
        return {"message": "ok"}

    j1, created1 = reg.start(kind="k", target="app:x", app_id="x", title="Slow", work=slow)
    j2, created2 = reg.start(kind="k", target="app:x", app_id="x", title="Slow", work=slow)
    assert created1 and not created2 and j1 is j2
    gate.set()
    assert _wait(lambda: j1.state == "succeeded")

    def boom(job):
        job.log("step one ok")
        raise am.NetworkUnavailable("Cannot reach registry.npmjs.org (offline).", hint="Check the connection.")

    j3, _ = reg.start(kind="k", target="app:y", app_id="y", title="Boom", work=boom, run_inline=True)
    d = j3.to_dict()
    assert d["state"] == "failed"
    assert d["error"]["reason"] == "network_unavailable"
    assert "step one ok" in d["details"] and "Cannot reach registry.npmjs.org" in d["details"]
    assert d["message"] == "Cannot reach registry.npmjs.org (offline)."


def test_job_cancel(tmp_path: Path) -> None:
    reg = am.JobRegistry(tmp_path)

    def work(job):
        job.cancel_event.set()
        job.check_cancel()
        return {}

    job, _ = reg.start(kind="k", target="t", app_id=None, title="C", work=work, run_inline=True)
    assert job.state == "cancelled"


# ---------------------------------------------------------------------------
# Install (download with progress, integrity, unpack) through a fake registry
# ---------------------------------------------------------------------------


def test_install_dependency_free_app_reports_bytes_and_percent(manager: am.AppsManager) -> None:
    spec = am.APP_BY_ID["code"]
    data = _publish(manager, spec, "1.2.3")
    job, created = manager.start_install("code", run_inline=True)
    d = job.to_dict()
    assert created and d["state"] == "succeeded", d["details"]
    assert d["percent"] == 100.0
    assert d["bytes_done"] == len(data) and d["bytes_total"] == len(data)
    assert [s["name"] for s in d["steps"]] == ["resolve", "download", "verify", "unpack"]
    assert manager.installed_version("code") == "1.2.3"
    assert manager.bin_path("code", "1.2.3").name == "cli.js"
    log = Path(d["log_path"]).read_text()
    assert "sha512 integrity verified" in log and "Downloading Code 1.2.3" in log


def test_install_rejects_tampered_tarball(manager: am.AppsManager) -> None:
    _publish(manager, am.APP_BY_ID["code"], "1.2.3", tamper=True)
    job, _ = manager.start_install("code", run_inline=True)
    d = job.to_dict()
    assert d["state"] == "failed"
    assert d["error"]["reason"] == "integrity_mismatch"
    assert "expected sha512-" in d["details"]
    assert manager.installed_version("code") is None
    assert not (manager.apps_root / "downloads" / "code-1.2.3.tgz").exists()


def test_install_offline_is_a_plain_message(manager: am.AppsManager) -> None:
    manager.net.offline = True  # type: ignore[attr-defined]
    job, _ = manager.start_install("observer", run_inline=True)
    d = job.to_dict()
    assert d["state"] == "failed" and d["error"]["reason"] == "network_unavailable"
    assert "npm registry" in d["message"] and "not reachable" in d["message"]
    ov = manager.overview()
    assert ov["registry"]["reachable"] is False
    row = next(a for a in ov["apps"] if a["id"] == "observer")
    assert row["install_available"] is False and "not reachable" in row["install_blocked_reason"]


def test_installs_refused_when_host_installs_are_off(tmp_path: Path) -> None:
    m = am.AppsManager(tmp_path / "d", urlopen=FakeNet(), install_allowed=lambda: False)
    with pytest.raises(am.InstallsNotAllowed):
        m.start_install("code")
    with pytest.raises(am.InstallsNotAllowed):
        m.start_node_install()


def test_npm_tarball_extraction_refuses_escape(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("package/../../evil.txt")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    p = tmp_path / "bad.tgz"
    p.write_bytes(buf.getvalue())
    with pytest.raises(am.AppsError, match="outside the install folder"):
        am._extract_npm_tarball(p, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


# ---------------------------------------------------------------------------
# Node runtime: wheel choice + unpack
# ---------------------------------------------------------------------------


def _pypi_doc() -> dict:
    def files(v):
        return [
            {"filename": f"nodejs_wheel_binaries-{v}-py2.py3-none-{tag}.whl", "packagetype": "bdist_wheel", "url": f"https://x/{v}/{tag}.whl", "size": 1, "digests": {"sha256": "00"}}
            for tag in ("macosx_13_0_arm64", "macosx_13_0_x86_64", "manylinux_2_28_x86_64", "musllinux_1_2_aarch64", "win_amd64")
        ]

    return {"releases": {"22.9.0": files("22.9.0"), "24.18.0": files("24.18.0"), "24.19.0": files("24.19.0"), "25.0.0": files("25.0.0"), "24.20.0rc1": files("24.20.0rc1")}}


def test_pick_node_wheel_per_platform() -> None:
    doc = _pypi_doc()
    v, f = am.pick_node_wheel(doc, system="darwin", machine="arm64")
    assert v == "24.19.0" and f["filename"].endswith("macosx_13_0_arm64.whl")
    assert am.pick_node_wheel(doc, system="linux", machine="x86_64", libc="glibc")[1]["filename"].endswith("manylinux_2_28_x86_64.whl")
    assert am.pick_node_wheel(doc, system="linux", machine="aarch64", libc="musl")[1]["filename"].endswith("musllinux_1_2_aarch64.whl")
    assert am.pick_node_wheel(doc, system="win32", machine="AMD64")[1]["filename"].endswith("win_amd64.whl")
    assert am.pick_node_wheel(doc, pin="24.18.0", system="darwin", machine="arm64")[0] == "24.18.0"
    with pytest.raises(am.AppsError, match="No Node.js"):
        am.pick_node_wheel(doc, system="linux", machine="aarch64", libc="glibc")


def test_extract_node_wheel_keeps_exec_bits(tmp_path: Path) -> None:
    whl = tmp_path / "n.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for name, mode in (("nodejs_wheel/bin/node", 0o755), ("nodejs_wheel/lib/node_modules/npm/bin/npm-cli.js", 0o644), ("nodejs_wheel/include/node/v8.h", 0o644), ("nodejs_wheel/__init__.py", 0o644)):
            zi = zipfile.ZipInfo(name)
            zi.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(zi, "#!/bin/sh\necho v24.0.0\n")
    node_rel, npm_rel = am._extract_node_wheel(whl, tmp_path / "out")
    assert node_rel == "bin/node" and npm_rel == "lib/node_modules/npm/bin/npm-cli.js"
    assert os.access(tmp_path / "out" / "bin" / "node", os.X_OK)
    assert not (tmp_path / "out" / "include").exists()


def test_managed_node_install_verifies_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    net = FakeNet()
    m = am.AppsManager(tmp_path / "d", urlopen=net, install_allowed=lambda: True)
    doc = _pypi_doc()
    monkeypatch.setattr(am, "_wheel_platform_match", lambda fn, **k: fn.endswith("macosx_13_0_arm64.whl"))
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, "w") as zf:
        zi = zipfile.ZipInfo("nodejs_wheel/bin/node")
        zi.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(zi, "#!/bin/sh\necho v24.19.0\n")
        zf.writestr("nodejs_wheel/lib/node_modules/npm/bin/npm-cli.js", "")
    data = wheel.getvalue()
    for f in doc["releases"]["24.19.0"]:
        f["digests"]["sha256"] = hashlib.sha256(data).hexdigest()
        f["size"] = len(data)
        net.routes[f["url"]] = data
    net.routes[m.pypi_url + "/nodejs-wheel-binaries/json"] = json.dumps(doc).encode()
    monkeypatch.setenv(am.ENV_NODE, "managed")
    job, _ = m.start_node_install(run_inline=True)
    d = job.to_dict()
    assert d["state"] == "succeeded", d["details"]
    assert d["bytes_done"] == len(data)
    st = m.node_status(refresh=True)
    assert st["available"] and st["source"] == "managed" and st["version"] == "24.19.0"
    # a corrupted download is refused and deleted
    for f in doc["releases"]["24.19.0"]:
        f["digests"]["sha256"] = "ff" * 32
    net.routes[m.pypi_url + "/nodejs-wheel-binaries/json"] = json.dumps(doc).encode()
    job2, _ = m.start_node_install(run_inline=True)
    assert job2.state == "failed" and job2.error["reason"] == "integrity_mismatch"


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


def test_allocate_port_prefers_then_scans() -> None:
    free = {3001: True, 3100: False, 3101: True}
    fn = lambda p, h: free.get(p, False)  # noqa: E731
    assert am.allocate_port(preferred=[3001], port_range=(3100, 3105), is_free=fn) == 3001
    assert am.allocate_port(preferred=[3001], port_range=(3100, 3105), taken=[3001], is_free=fn) == 3101
    assert am.allocate_port(preferred=[3001], port_range=(3100, 3105), restrict_to_range=True, is_free=fn) == 3101
    with pytest.raises(am.NoFreePort):
        am.allocate_port(preferred=[], port_range=(3100, 3100), is_free=fn)


def test_port_is_free_sees_a_listener() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert not am.port_is_free(port)
    finally:
        s.close()
    assert am.port_is_free(port)


def test_parse_port_range() -> None:
    assert am.parse_port_range("18830-18839") == (18830, 18839)
    assert am.parse_port_range("") is None
    with pytest.raises(am.AppsError):
        am.parse_port_range("abc")


# ---------------------------------------------------------------------------
# Process supervision (real processes, fake node)
# ---------------------------------------------------------------------------


def test_launch_stop_and_env(manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(manager, am.APP_BY_ID["code"], "1.0.0")
    job, _ = manager.start_install("code", run_inline=True)
    assert job.state == "succeeded", job.details
    port = _free_port()
    monkeypatch.setenv(am.ENV_PORTS, f"{port}-{port}")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "secret-token-must-not-leak")
    row = manager.launch("code", gateway_url="http://127.0.0.1:18823")
    assert row["running"] and row["port"] == port and row["url"] == f"http://127.0.0.1:{port}/"
    assert manager.is_enabled("code")
    import urllib.request

    body = urllib.request.urlopen(row["url"], timeout=5).read().decode()
    assert body == "ok http://127.0.0.1:18823 token=-"  # gateway URL configured, token scrubbed
    pid = row["pid"]
    assert json.loads(manager.pid_path("code").read_text())["pid"] == pid
    row = manager.stop("code")
    assert row["status"] == "stopped" and not row["running"]
    assert not manager.is_enabled("code")
    assert am.port_is_free(port)
    assert not manager.pid_path("code").exists()


def test_crash_restarts_then_crash_loop(manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(manager, am.APP_BY_ID["code"], "1.0.0")
    manager.start_install("code", run_inline=True)
    monkeypatch.setenv(am.ENV_PORTS, f"{_free_port()}-{_free_port()}")
    row = manager.launch("code")
    first_pid = row["pid"]
    proc = manager._procs["code"]
    os.kill(first_pid, 15)  # an unexpected exit
    assert _wait(lambda: proc.status == "running" and proc.pid != first_pid and proc.alive())
    snap = manager.app_row(am.APP_BY_ID["code"])
    assert snap["restarts_last_minute"] == 1 and snap["running"]
    for _ in range(am.MAX_RESTARTS):
        pid = proc.pid
        assert _wait(lambda: proc.alive() and proc.status == "running")
        os.kill(pid, 15)
        assert _wait(lambda: proc.pid != pid or proc.status == "crash_loop")
    assert _wait(lambda: proc.status == "crash_loop")
    snap = manager.app_row(am.APP_BY_ID["code"])
    assert not snap["running"] and "not restarting" in snap["last_error"]


def test_launch_failure_carries_the_app_log(manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(manager, am.APP_BY_ID["code"], "1.0.0")
    manager.start_install("code", run_inline=True)
    (manager.package_dir("code", "1.0.0") / "bin" / "mode").write_text("exit")
    monkeypatch.setenv(am.ENV_PORTS, f"{_free_port()}-{_free_port()}")
    with pytest.raises(am.LaunchFailed) as ei:
        manager.launch("code")
    assert "boom: refusing to start" in (ei.value.details or "")
    time.sleep(0.5)
    proc = manager._procs["code"]
    assert proc.status == "crashed" and len(proc.restarts) == 0  # reported, not retried
    assert manager.app_log_path("code").read_text().count("=== ") == 1  # started once


def test_launch_requires_install(manager: am.AppsManager) -> None:
    with pytest.raises(am.NotInstalled):
        manager.launch("flow")
    with pytest.raises(am.UnknownApp):
        manager.launch("nope")


def test_autostart_starts_enabled_apps_and_reaps_orphans(manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(manager, am.APP_BY_ID["code"], "1.0.0")
    manager.start_install("code", run_inline=True)
    port = _free_port()
    monkeypatch.setenv(am.ENV_PORTS, f"{port}-{port}")
    manager.launch("code")
    old_pid = manager._procs["code"].pid
    # simulate a gateway that died without stopping its app: forget the process
    orphan = manager._procs.pop("code")
    orphan._stop_requested = True  # its supervisor "died" with the old gateway
    assert am._pid_command(old_pid) is not None
    outcomes = manager.autostart(gateway_url="http://127.0.0.1:18823")
    assert outcomes == [{"app_id": "code", "ok": True, "url": f"http://127.0.0.1:{port}/"}]
    assert _wait(lambda: orphan.proc.poll() is not None)  # the leftover was stopped before relaunch
    assert manager._procs["code"].pid != old_pid and manager._procs["code"].alive()


def test_child_env_scrub() -> None:
    env = am._scrubbed_child_env(
        {"PATH": "/bin", "ABSTRACTGATEWAY_AUTH_TOKEN": "t", "ABSTRACTGATEWAY_SESSIONS_FILE": "/s", "ABSTRACTCORE_CONFIG_FILE": "/c", "OPENAI_API_KEY": "k", "GITHUB_TOKEN": "g", "HOME": "/h"}
    )
    assert env == {"PATH": "/bin", "HOME": "/h"}


# ---------------------------------------------------------------------------
# Handover codes
# ---------------------------------------------------------------------------


def test_handover_code_is_single_use_and_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = am.AppsManager(tmp_path / "d", urlopen=FakeNet())
    code = m.mint_handover("code", "principal", host="127.0.0.1")
    assert m.redeem_handover(code) == ("code", "principal", "127.0.0.1")
    assert m.redeem_handover(code) is None
    code2 = m.mint_handover("code", "principal", host="127.0.0.1")
    monkeypatch.setattr(am, "_now", lambda: time.time() + am.HANDOVER_TTL_S + 1)
    assert m.redeem_handover(code2) is None
    assert m.redeem_handover("") is None
