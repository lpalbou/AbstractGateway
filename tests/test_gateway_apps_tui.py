"""Terminal apps (mission Y): `interfaces[]` on the app rows, the release-binary
install of Code's terminal app, `launch-tui` (gateway machine only), and the
one-time terminal sign-in handover.

No real network (a fake urlopen serves the GitHub release), no real terminal
(`AppsManager.terminal_opener` is replaced), no real home directory."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.error
from pathlib import Path
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient

from abstractgateway import apps_manager as am
from abstractgateway import tui_signin

_TOKEN = "apps-tui-test-token-0123456789abcdef"
_REPO_API = f"{am.GITHUB_API}/repos/lpalbou/abstractcode/releases?per_page=30"
_FAKE_BIN = "#!/bin/sh\ncase \"$1\" in --help) echo 'abstractcode — AbstractCode on AbstractTUI (gateway client)';; --version) echo 'abstractcode 0.9.9';; esac\n"


class _Resp(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.status = 200
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeNet:
    def __init__(self) -> None:
        self.routes: Dict[str, bytes] = {}
        self.calls: List[str] = []
        self.offline = False

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        if self.offline:
            raise urllib.error.URLError("offline")
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return _Resp(self.routes[url])


def _tarball(binary: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("abstractcode")
        info.size = len(binary)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(binary))
    return buf.getvalue()


def _publish(net: FakeNet, *, version: str = "0.9.9", target: str = "aarch64-apple-darwin", sums_hex: str = "", digest: str = "", binary: str = _FAKE_BIN) -> str:
    """A GitHub release like abstractcode's release.yml makes (plus a web-v
    tag and a prerelease that must be ignored). Returns the asset's sha256."""
    tag = f"v{version}"
    name = f"abstractcode-{tag}-{target}.tar.gz"
    blob = _tarball(binary.replace("0.9.9", version).encode())
    real = hashlib.sha256(blob).hexdigest()
    base = f"https://github.example/lpalbou/abstractcode/releases/download/{tag}"
    net.routes[f"{base}/{name}"] = blob
    net.routes[f"{base}/SHA256SUMS"] = f"{sums_hex or real}  {name}\n{'0' * 64}  abstractcode-{tag}-x86_64-pc-windows-msvc.zip\n".encode()
    releases = [
        {"tag_name": "web-v9.9.9", "draft": False, "prerelease": False, "assets": []},
        {"tag_name": "v99.0.0", "draft": False, "prerelease": True, "assets": []},
        {"tag_name": "v0.0.1", "draft": False, "prerelease": False, "assets": []},
        {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "html_url": f"https://github.example/lpalbou/abstractcode/releases/tag/{tag}",
            "assets": [
                {"name": name, "browser_download_url": f"{base}/{name}", "size": len(blob), "digest": digest or f"sha256:{real}"},
                {"name": "SHA256SUMS", "browser_download_url": f"{base}/SHA256SUMS", "size": 200},
            ],
        },
    ]
    net.routes[_REPO_API] = json.dumps(releases).encode()
    return real


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch HOME and a PATH without any real `abstractcode`."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(am, "release_target", lambda **kw: "aarch64-apple-darwin")
    return h


@pytest.fixture()
def mgr(tmp_path: Path, home: Path):
    net = FakeNet()
    m = am.AppsManager(tmp_path / "data", urlopen=net)
    opened: List[List[str]] = []
    m.terminal_opener = lambda argv: opened.append(list(argv))
    return m, net, opened


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_release_target_matches_the_release_workflow_matrix() -> None:
    assert am.release_target(system="darwin", machine="arm64") == "aarch64-apple-darwin"
    assert am.release_target(system="darwin", machine="x86_64") == "x86_64-apple-darwin"
    assert am.release_target(system="linux", machine="x86_64", libc="glibc") == "x86_64-unknown-linux-gnu"
    assert am.release_target(system="linux", machine="aarch64", libc="glibc") == "aarch64-unknown-linux-gnu"
    assert am.release_target(system="win32", machine="AMD64") == "x86_64-pc-windows-msvc"
    # no prebuilt binary: musl Linux, Windows on ARM, other CPUs
    assert am.release_target(system="linux", machine="x86_64", libc="musl") is None
    assert am.release_target(system="win32", machine="ARM64") is None
    assert am.release_target(system="linux", machine="riscv64", libc="glibc") is None


def test_pick_release_ignores_web_tags_prereleases_and_drafts() -> None:
    rels = [
        {"tag_name": "web-v1.0.0", "assets": []},
        {"tag_name": "v2.0.0", "prerelease": True, "assets": []},
        {"tag_name": "v1.9.0", "draft": True, "assets": []},
        {"tag_name": "v0.5.1", "assets": [{"name": "SHA256SUMS", "browser_download_url": "u"}]},
        {"tag_name": "v0.5.0", "assets": []},
    ]
    got = am.pick_tui_release(rels, am.CODE_TUI)
    assert got["version"] == "0.5.1" and "SHA256SUMS" in got["assets"]
    assert am.pick_tui_release([], am.CODE_TUI) is None


def test_parse_sha256sums() -> None:
    text = f"{'a' * 64}  one.tar.gz\n{'B' * 64} *two.zip\njunk\n"
    assert am.parse_sha256sums(text) == {"one.tar.gz": "a" * 64, "two.zip": "b" * 64}


def test_launcher_script_holds_a_one_time_code_and_deletes_itself() -> None:
    text = am.tui_launch_script_text(python="/py", helper="/h/tui_signin.py", binary="/b/abstractcode", gateway_url="http://127.0.0.1:1", app_id="code", code="CODE123", windows=False)
    assert text.startswith("#!/bin/sh\n") and 'rm -f -- "$0"' in text
    assert "CODE123" in text and "export ABSTRACTGATEWAY_TUI_HANDOVER" in text
    assert "-I /h/tui_signin.py" in text and "--gateway http://127.0.0.1:1" in text
    win = am.tui_launch_script_text(python="C:\\py.exe", helper="C:\\h.py", binary="C:\\b.exe", gateway_url="http://127.0.0.1:1", app_id="code", code="CODE123", windows=True)
    assert 'del "%~f0"' in win and 'set "ABSTRACTGATEWAY_TUI_HANDOVER=CODE123"' in win


def test_terminal_argv_per_platform(tmp_path: Path) -> None:
    script = tmp_path / "s.sh"
    assert am.terminal_argv(script, system="darwin") == ("Terminal", ["open", "-a", "Terminal", str(script)])
    assert am.terminal_argv(script, system="win32")[1][:4] == ["cmd", "/c", "start", ""]
    with pytest.raises(am.NoTerminal):
        am.terminal_argv(script, system="linux", environ={})
    which = {"gnome-terminal": "/usr/bin/gnome-terminal"}.get
    assert am.terminal_argv(script, system="linux", which=which, environ={"DISPLAY": ":0"}) == ("gnome-terminal", ["/usr/bin/gnome-terminal", "--", str(script)])
    with pytest.raises(am.NoTerminal):
        am.terminal_argv(script, system="linux", which=lambda n: None, environ={"WAYLAND_DISPLAY": "w"})


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def test_a_same_named_script_on_path_is_not_the_terminal_app(mgr, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m, _net, _ = mgr
    bindir = home / "bin"
    bindir.mkdir()
    fake = bindir / "abstractcode"
    fake.write_text("#!/usr/bin/env python3\nprint('abstractcode 0.3.8')\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    assert m.tui_status(am.CODE_TUI)["installed"] is False  # PyPI's Python `abstractcode`, not the TUI


def test_presence_needs_the_help_marker(mgr, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m, _net, _ = mgr
    cargo = home / ".cargo" / "bin"
    cargo.mkdir(parents=True)
    b = cargo / "abstractcode"
    b.write_text(_FAKE_BIN)
    b.chmod(0o755)
    monkeypatch.setattr(am, "_is_script", lambda p: False)  # stands in for a native binary
    st = m.tui_status(am.CODE_TUI)
    assert st == {"installed": True, "version": "0.9.9", "source": "path", "path": str(b)}
    b.write_text(_FAKE_BIN.replace("AbstractTUI", "something else"))
    os.utime(b, (1, 1))
    assert m.tui_status(am.CODE_TUI)["installed"] is False


# ---------------------------------------------------------------------------
# Install (release binary)
# ---------------------------------------------------------------------------


def test_install_downloads_verifies_and_places_the_binary(mgr, monkeypatch: pytest.MonkeyPatch) -> None:
    m, net, _ = mgr
    sha = _publish(net)
    job, created = m.start_tui_install("code", run_inline=True)
    d = job.to_dict()
    assert created and d["state"] == "succeeded", d
    target = m.bin_dir / "abstractcode"
    assert target.is_file() and os.access(target, os.X_OK)
    assert d["result"]["version"] == "0.9.9"
    rec = m.app_state("code")["tui"]
    assert rec["sha256"] == sha and rec["tag"] == "v0.9.9"
    log = "\n".join(d["log_tail"])
    assert "GitHub digest agrees" in log and "sha256 verified" in log and "--version -> abstractcode 0.9.9" in log
    assert not list(m.bin_dir.glob(".abstractcode.*")), "no staging leftovers"
    monkeypatch.setattr(am, "_is_script", lambda p: False)
    assert m.tui_status(am.CODE_TUI)["source"] == "gateway"


def test_install_refuses_a_checksum_mismatch_and_places_nothing(mgr) -> None:
    m, net, _ = mgr
    _publish(net, sums_hex="f" * 64, digest="sha256:" + "f" * 64)
    job, _ = m.start_tui_install("code", run_inline=True)
    d = job.to_dict()
    assert d["state"] == "failed" and d["error"]["reason"] == "integrity_mismatch", d
    assert not (m.bin_dir / "abstractcode").exists()


def test_install_refuses_when_github_digest_and_sums_disagree(mgr) -> None:
    m, net, _ = mgr
    _publish(net, digest="sha256:" + "e" * 64)
    d = m.start_tui_install("code", run_inline=True)[0].to_dict()
    assert d["state"] == "failed" and "disagrees" in d["error"]["message"]
    assert not (m.bin_dir / "abstractcode").exists()


def test_install_refuses_a_binary_that_does_not_report_its_version(mgr) -> None:
    m, net, _ = mgr
    _publish(net, binary="#!/bin/sh\necho 'something else'\n")
    d = m.start_tui_install("code", run_inline=True)[0].to_dict()
    assert d["state"] == "failed" and "--version" in d["error"]["message"]
    assert not (m.bin_dir / "abstractcode").exists()


def test_toolchain_case_is_not_installable_and_says_why(mgr, monkeypatch: pytest.MonkeyPatch) -> None:
    m, net, _ = mgr
    _publish(net)
    monkeypatch.setattr(am, "release_target", lambda **kw: None)  # e.g. musl Linux
    t = m.tui_interface(am.CODE_TUI, release=m.tui_release(am.CODE_TUI))
    assert t["install_available"] is False and t["install_method"] == "cargo"
    assert t["install_blocked_reason"].startswith("Needs the Rust toolchain")
    assert t["install_command"] == "cargo install abstractcode"
    with pytest.raises(am.ToolchainRequired) as ei:
        m.start_tui_install("code")
    assert ei.value.payload()["command"] == "cargo install abstractcode"


def test_gateway_console_tui_is_never_installable_here(mgr) -> None:
    m, _net, _ = mgr
    ct = m.console_tui_interface(caller={"gateway_url": "http://127.0.0.1:9"})
    assert ct["install_available"] is False and ct["install_method"] == "cargo"
    assert ct["install_command"] == "cargo install abstractgateway-console"
    assert ct["command"] == "abstractgateway-console --url http://127.0.0.1:9"
    assert "Rust toolchain" in ct["install_blocked_reason"]


# ---------------------------------------------------------------------------
# interfaces[] on the rows
# ---------------------------------------------------------------------------


def test_every_row_has_a_web_interface_and_only_code_a_terminal_one(mgr) -> None:
    m, net, _ = mgr
    _publish(net)
    net.offline = False
    ov = m.overview(check_latest=False, caller={"local": True, "admin": True, "gateway_url": "http://127.0.0.1:18890"})
    kinds = {a["id"]: [i["kind"] for i in a["interfaces"]] for a in ov["apps"]}
    assert kinds == {"flow": ["web"], "code": ["web", "tui"], "observer": ["web"], "continuum": ["web"], "entity": ["web"]}
    for a in ov["apps"]:
        web = a["interfaces"][0]
        assert web["installed"] == a["installed"] and web["install_available"] == a["install_available"]
        assert web["version"] == a["version"] and web["install_method"] == "npm"
        assert set(web) >= {"launch_available", "launch_blocked_reason", "command"}
    tui = next(a for a in ov["apps"] if a["id"] == "code")["interfaces"][1]
    assert set(tui) >= {"kind", "installed", "version", "install_available", "install_method", "launch_available", "launch_blocked_reason", "command"}
    assert tui["installed"] is False and tui["install_available"] is True and tui["install_method"] == "release_binary"
    assert tui["launch_available"] is False
    assert ov["console_tui"]["binary"] == "abstractgateway-console"


def test_remote_or_non_admin_callers_get_the_command_not_a_launch(mgr, monkeypatch: pytest.MonkeyPatch) -> None:
    m, net, _ = mgr
    monkeypatch.setattr(m, "tui_status", lambda tui: {"installed": True, "version": "0.9.9", "source": "gateway", "path": "/data/apps/bin/abstractcode"})
    here = m.tui_interface(am.CODE_TUI, caller={"local": True, "admin": True, "gateway_url": "http://127.0.0.1:8080"})
    assert here["launch_available"] is True and here["launch_mode"] == "terminal"
    assert here["command"] == "/data/apps/bin/abstractcode --gateway http://127.0.0.1:8080"
    away = m.tui_interface(am.CODE_TUI, caller={"local": False, "admin": True, "gateway_url": "http://10.0.0.5:8080"})
    assert away["launch_available"] is False and away["launch_mode"] == "copy"
    assert away["command"] == "abstractcode --gateway http://10.0.0.5:8080"  # the name, not this machine's path
    assert away["signin_command"] == "abstractcode login --gateway http://10.0.0.5:8080 --token <your token>"
    user = m.tui_interface(am.CODE_TUI, caller={"local": True, "admin": False, "gateway_url": "http://127.0.0.1:8080"})
    assert user["launch_available"] is False and "admin" in user["launch_blocked_reason"]


# ---------------------------------------------------------------------------
# Routes: launch-tui, install-tui, the terminal handover
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    net = FakeNet()
    m = am.AppsManager(tmp_path / "runtime", urlopen=net, install_allowed=lambda: True)
    opened: List[List[str]] = []
    m.terminal_opener = lambda argv: opened.append(list(argv))
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: m)
    monkeypatch.setattr(m, "tui_status", lambda tui: {"installed": True, "version": "0.9.9", "source": "gateway", "path": str(tmp_path / "runtime" / "apps" / "bin" / "abstractcode")})
    from abstractgateway.app import app

    local = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}", "host": "127.0.0.1:18890"}, client=("127.0.0.1", 50001))
    remote = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}", "host": "192.168.1.20:18890"}, client=("192.168.1.30", 50002))
    return m, net, opened, local, remote, app


def test_launch_tui_from_another_computer_returns_the_command_and_opens_nothing(api) -> None:
    m, _net, opened, _local, remote, app = api
    r = remote.post("/api/gateway/apps/code/launch-tui", json={})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["reason"] == "not_on_gateway_machine"
    assert body["command"] == "abstractcode --gateway http://192.168.1.20:18890"
    assert _TOKEN not in r.text
    assert opened == [] and m._tui_handover == {}
    # a loopback peer behind a proxy is not "on the gateway machine" either
    proxied = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}", "host": "127.0.0.1:18890", "x-forwarded-for": "203.0.113.9"}, client=("127.0.0.1", 50003))
    assert proxied.post("/api/gateway/apps/code/launch-tui", json={}).json()["reason"] == "not_on_gateway_machine"
    assert opened == []
    # a loopback peer with a non-loopback Host (a LAN name) neither
    lan_host = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}", "host": "192.168.1.20:18890"}, client=("127.0.0.1", 50004))
    assert lan_host.post("/api/gateway/apps/code/launch-tui", json={}).status_code == 409
    assert opened == []


def test_launch_tui_opens_a_terminal_with_a_one_time_code_and_no_token(api, tmp_path: Path) -> None:
    m, _net, opened, local, _remote, _app = api
    r = local.post("/api/gateway/apps/code/launch-tui", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["interface"] == "tui" and body["expires_in_s"] == 120
    assert len(opened) == 1
    argv = opened[0]
    script = Path(argv[-1])
    if sys.platform == "darwin":
        assert argv[:3] == ["open", "-a", "Terminal"]
    text = script.read_text()
    assert oct(script.stat().st_mode & 0o777) == "0o700"
    (code,) = list(m._tui_handover)
    assert code in text
    assert _TOKEN not in text and "agtui_" not in text and "ABSTRACTCODE_GATEWAY_TOKEN" not in text
    assert "--gateway http://127.0.0.1:18890" in text

    # the handover: loopback only, once
    from abstractgateway.app import app

    pub_remote = TestClient(app, client=("192.168.1.30", 50010))
    assert pub_remote.post("/apps/tui-handover", json={"code": code}).status_code == 403
    assert code in m._tui_handover, "a refused peer must not burn the code"
    pub_local = TestClient(app, client=("127.0.0.1", 50011))
    got = pub_local.post("/apps/tui-handover", json={"code": code})
    assert got.status_code == 200, got.text
    assert got.headers.get("cache-control") == "no-store"
    h = got.json()
    assert h["token_env"] == "ABSTRACTCODE_GATEWAY_TOKEN" and h["gateway_url"] == "http://127.0.0.1:18890" and h["gateway_flag"] == "--gateway"
    token = h["token"]
    assert token.startswith("agtui_") and token != _TOKEN
    again = pub_local.post("/apps/tui-handover", json={"code": code})
    assert again.status_code == 410 and again.json()["reason"] == "handover_expired"

    # the token works from this machine only
    ok = TestClient(app, headers={"Authorization": f"Bearer {token}"}, client=("127.0.0.1", 50012)).get("/api/gateway/apps?latest=false")
    assert ok.status_code == 200, ok.text
    far = TestClient(app, headers={"Authorization": f"Bearer {token}"}, client=("192.168.1.30", 50013)).get("/api/gateway/apps?latest=false")
    assert far.status_code == 401

    # and it is written nowhere on disk
    for f in Path(tmp_path).rglob("*"):
        if f.is_file():
            assert token.encode() not in f.read_bytes(), f


def test_browser_and_terminal_codes_are_not_interchangeable(api) -> None:
    m, _net, _opened, _local, _remote, app = api
    from abstractgateway.security.principal import local_admin_principal

    web_code = m.mint_handover("code", local_admin_principal(), host="127.0.0.1")
    pub_local = TestClient(app, client=("127.0.0.1", 50020))
    assert pub_local.post("/apps/tui-handover", json={"code": web_code}).status_code == 410
    tui_code = m.mint_tui_handover("code", local_admin_principal(), gateway_url="http://127.0.0.1:1")
    assert m.redeem_handover(tui_code) is None


def test_terminal_token_acts_as_the_caller_never_more(api) -> None:
    m, _net, _opened, _local, _remote, app = api
    from abstractgateway.security.principal import GatewayPrincipal

    alice = GatewayPrincipal(user_id="alice", tenant_id="default", roles=("user",), source="session")
    token = m.issue_tui_token("code", alice)
    c = TestClient(app, headers={"Authorization": f"Bearer {token}"}, client=("127.0.0.1", 50030))
    me = c.get("/api/gateway/me")
    assert me.status_code == 200, me.text
    assert me.json()["principal"]["user_id"] == "alice"
    assert c.post("/api/gateway/apps/code/install-tui", json={}).status_code == 403  # admin route


def test_install_and_launch_tui_are_admin_routes(api) -> None:
    from abstractgateway.security.authorization import gateway_route_authorization_requirement

    for path in ("/api/gateway/apps/code/install-tui", "/api/gateway/apps/code/launch-tui"):
        req = gateway_route_authorization_requirement(path, "POST")
        assert req is not None and req.resource == "apps" and req.required_role == "admin", path


def test_install_tui_route_toolchain_case_is_a_409_with_the_command(api, monkeypatch: pytest.MonkeyPatch) -> None:
    _m, _net, _opened, local, _remote, _app = api
    monkeypatch.setattr(am, "release_target", lambda **kw: None)
    r = local.post("/api/gateway/apps/code/install-tui", json={})
    assert r.status_code == 409
    assert r.json()["reason"] == "toolchain_required" and r.json()["command"] == "cargo install abstractcode"
    r = local.post("/api/gateway/apps/flow/install-tui", json={})
    assert r.status_code == 404 and "browser only" in r.json()["message"]


def test_overview_route_is_caller_aware(api) -> None:
    _m, _net, _opened, local, remote, _app = api
    t_local = next(a for a in local.get("/api/gateway/apps?latest=false").json()["apps"] if a["id"] == "code")["interfaces"][1]
    t_remote = next(a for a in remote.get("/api/gateway/apps?latest=false").json()["apps"] if a["id"] == "code")["interfaces"][1]
    assert t_local["launch_available"] is True and t_local["launch_mode"] == "terminal"
    assert t_remote["launch_available"] is False and t_remote["command"] == "abstractcode --gateway http://192.168.1.20:18890"


def test_launch_tui_not_installed_is_409_with_the_install_command(api, monkeypatch: pytest.MonkeyPatch) -> None:
    m, _net, opened, local, _remote, _app = api
    monkeypatch.setattr(m, "tui_status", lambda tui: {"installed": False, "version": None, "source": None, "path": None})
    r = local.post("/api/gateway/apps/code/launch-tui", json={})
    assert r.status_code == 409 and r.json()["reason"] == "not_installed"
    assert r.json()["install_command"] == "cargo install abstractcode"
    assert opened == [] and m._tui_handover == {}


def test_a_terminal_that_fails_to_open_burns_the_code_and_the_script(api) -> None:
    m, _net, _opened, local, _remote, _app = api

    def broken(argv):
        raise OSError("no such program")

    m.terminal_opener = broken
    r = local.post("/api/gateway/apps/code/launch-tui", json={})
    assert r.status_code == 500 and r.json()["reason"] == "launch_failed" and r.json()["command"]
    assert m._tui_handover == {}
    assert not list((m.apps_root / "terminal").glob("open-*"))


# ---------------------------------------------------------------------------
# tui_signin.py (the terminal side)
# ---------------------------------------------------------------------------


def test_signin_helper_puts_the_token_in_the_environment_never_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}
    monkeypatch.setenv(tui_signin.HANDOVER_ENV, "the-code")
    monkeypatch.setattr(tui_signin, "redeem", lambda gw, code, **kw: {"token": "agtui_SECRET", "token_env": "ABSTRACTCODE_GATEWAY_TOKEN", "url_env": "ABSTRACTCODE_GATEWAY_URL", "gateway_url": gw, "gateway_flag": "--gateway", "user": "admin"} if code == "the-code" else {})
    monkeypatch.setattr(tui_signin.sys, "platform", "darwin")

    def execve(path, argv, env):
        seen.update(path=path, argv=argv, env=env)
        raise SystemExit(0)

    monkeypatch.setattr(tui_signin.os, "execve", execve)
    with pytest.raises(SystemExit):
        tui_signin.main(["--gateway", "http://127.0.0.1:18890", "--app", "code", "--", "/bin/abstractcode"])
    assert seen["argv"] == ["/bin/abstractcode", "--gateway", "http://127.0.0.1:18890"]
    assert all("agtui_SECRET" not in a for a in seen["argv"])
    assert seen["env"]["ABSTRACTCODE_GATEWAY_TOKEN"] == "agtui_SECRET"
    assert tui_signin.HANDOVER_ENV not in seen["env"], "the code does not leak into the terminal app"


def test_signin_helper_without_a_code_does_not_start_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tui_signin.HANDOVER_ENV, raising=False)
    monkeypatch.setattr(tui_signin.os, "execve", lambda *a: pytest.fail("must not start"))
    assert tui_signin.main(["--gateway", "http://127.0.0.1:1", "--app", "code", "--", "/bin/x"]) == 1


# ---------------------------------------------------------------------------
# Terminal parity: `abstractgateway apps install-tui | tui-command | list`
# ---------------------------------------------------------------------------


def test_tui_command_route_makes_a_one_use_launcher_and_opens_nothing(api) -> None:
    m, _net, opened, local, remote, app = api
    r = remote.post("/api/gateway/apps/code/tui-command", json={})
    assert r.status_code == 409 and r.json()["reason"] == "not_on_gateway_machine" and r.json()["command"]
    assert m._tui_handover == {}
    r = local.post("/api/gateway/apps/code/tui-command", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert opened == [], "tui-command never opens a window"
    script = Path(d["signin_command"].strip("'\""))
    text = script.read_text()
    (code,) = list(m._tui_handover)
    assert code in text and _TOKEN not in text and "agtui_" not in text
    assert d["command"].endswith("--gateway http://127.0.0.1:18890") and d["expires_in_s"] == 120
    pub = TestClient(app, client=("127.0.0.1", 50040))
    assert pub.post("/apps/tui-handover", json={"code": code}).status_code == 200
    assert pub.post("/apps/tui-handover", json={"code": code}).status_code == 410


class _Answer:
    def __init__(self, status: int, data):
        self.status, self.data = status, data
        self.ok = 200 <= status < 300

    def message(self) -> str:
        return str((self.data or {}).get("message") or f"HTTP {self.status}")


class _FakeTransport:
    url = "http://127.0.0.1:18890"

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def call(self, method, path, body=None, *, timeout=None):
        self.calls.append((method, path, body))
        v = self.answers[(method, path.split("?")[0])]
        v = v() if callable(v) else v
        return _Answer(v[0], v[1])


def _cli(argv, transport, monkeypatch, capsys):
    import abstractgateway.apps_cli as cli

    monkeypatch.setattr(cli, "_transport", lambda args: transport)
    monkeypatch.setattr(cli, "_POLL_S", 0.0)
    from abstractgateway.cli import main

    with pytest.raises(SystemExit) as ei:
        main(argv)
    out = capsys.readouterr()
    return ei.value.code, out.out, out.err


def test_cli_install_tui_follows_the_job_and_prints_the_toolchain_refusal(monkeypatch, capsys) -> None:
    jobs = iter([
        {"id": "j1", "state": "running", "percent": 41.0, "message": "Downloading Code terminal app 0.5.1: 1.3 MB of 3.1 MB (41%)", "bytes_done": 1270000, "bytes_total": 3086992},
        {"id": "j1", "state": "succeeded", "percent": 100.0, "message": "done", "result": {"message": "Code's terminal app 0.5.1 is installed."}},
    ])
    t = _FakeTransport({
        ("POST", "/apps/code/install-tui"): (200, {"ok": True, "job": {"id": "j1", "state": "queued", "percent": 0, "message": "Waiting"}}),
        ("GET", "/apps/jobs/j1"): lambda: (200, {"ok": True, "job": next(jobs)}),
    })
    code, out, err = _cli(["apps", "install-tui", "code"], t, monkeypatch, capsys)
    assert code == 0 and t.calls[0] == ("POST", "/apps/code/install-tui", {})
    assert "1.3 MB of 3.1 MB" in err and "done: Code's terminal app 0.5.1 is installed." in out
    refused = _FakeTransport({("POST", "/apps/code/install-tui"): (409, {"ok": False, "reason": "toolchain_required", "message": "Needs the Rust toolchain: there is no prebuilt Code terminal app for this computer (linux/riscv64).", "hint": "Install it with `cargo install abstractcode` (needs Rust 1.87 or newer).", "command": "cargo install abstractcode"})})
    code, _out, err = _cli(["apps", "install-tui", "code"], refused, monkeypatch, capsys)
    assert code == 2
    assert "error: Needs the Rust toolchain" in err and "run: cargo install abstractcode" in err


def test_cli_tui_command_prints_the_one_time_line_and_never_a_token(monkeypatch, capsys) -> None:
    t = _FakeTransport({("POST", "/apps/code/tui-command"): (200, {"ok": True, "signin_command": "'/d/apps/terminal/open-code-abc.command'", "command": "/d/apps/bin/abstractcode --gateway http://127.0.0.1:18890", "expires_in_s": 120})})
    code, out, _ = _cli(["apps", "tui-command", "code"], t, monkeypatch, capsys)
    assert code == 0
    assert "  '/d/apps/terminal/open-code-abc.command'" in out and "within 120 s" in out
    assert "  /d/apps/bin/abstractcode --gateway http://127.0.0.1:18890" in out
    assert "agtui_" not in out and "token" not in out.lower().replace("<your token>", "")
    remote = _FakeTransport({("POST", "/apps/code/tui-command"): (409, {"ok": False, "reason": "not_on_gateway_machine", "message": "only for the gateway machine", "command": "abstractcode --gateway http://10.0.0.5:8080", "signin_command": "abstractcode login --gateway http://10.0.0.5:8080 --token <your token>"})})
    code, _, err = _cli(["apps", "tui-command", "code"], remote, monkeypatch, capsys)
    assert code == 2 and "run: abstractcode --gateway http://10.0.0.5:8080" in err and "sign in there once:" in err


def test_cli_list_shows_the_terminal_interface(monkeypatch, capsys) -> None:
    def row(tui):
        return {"id": "code", "version": "0.4.2", "latest_version": "0.4.2", "status": "running", "url": "http://127.0.0.1:3002/", "running": True, "interfaces": [{"kind": "web"}, tui]}

    base = {"runtime": {"node": {"message": "Node.js 24", "path": None}}, "registry": {"reachable": True},
            "console_tui": {"kind": "tui", "installed": False, "install_method": "cargo", "install_blocked_reason": "Needs the Rust toolchain: source only.", "install_command": "cargo install abstractgateway-console", "command": "abstractgateway-console --url http://127.0.0.1:18890"}}
    for tui, want in (
        ({"kind": "tui", "installed": True, "version": "0.5.1", "source": "gateway", "command": "/d/bin/abstractcode --gateway http://127.0.0.1:18890"}, "terminal: 0.5.1 (installed by the gateway) · run: /d/bin/abstractcode --gateway http://127.0.0.1:18890; open signed in: abstractgateway apps tui-command code"),
        ({"kind": "tui", "installed": False, "install_available": True, "install_method": "release_binary"}, "terminal: not installed · install: abstractgateway apps install-tui code"),
        ({"kind": "tui", "installed": False, "install_available": False, "install_method": "cargo", "install_blocked_reason": "Needs the Rust toolchain: no prebuilt binary.", "install_command": "cargo install abstractcode"}, "terminal: not installed · Needs the Rust toolchain: no prebuilt binary. · cargo install abstractcode"),
    ):
        t = _FakeTransport({("GET", "/apps"): (200, dict(base, apps=[row(tui)]))})
        code, out, _ = _cli(["apps", "list", "--no-latest"], t, monkeypatch, capsys)
        assert code == 0 and want in out, out
        assert "gateway console (terminal): terminal: not installed · Needs the Rust toolchain: source only. · cargo install abstractgateway-console" in out


def test_console_terminal_action_sits_in_the_card_action_row() -> None:
    """Mission GG: the terminal version is no longer its own block in the card
    ("Also runs in your terminal", glyph, pill, sentence). Installed -> an
    "Open in Terminal" button next to Open; installable -> a quiet "Install
    for Terminal"; the Rust-toolchain and other-computer cases say nothing
    in the plain view and show their one-line command + Copy only with
    Technical details on."""
    from abstractgateway.console_ui import CONSOLE_UI_JS

    parts = CONSOLE_UI_JS[CONSOLE_UI_JS.index("function appTuiParts(app, techOn) {"):CONSOLE_UI_JS.index("function appRowById(id)")]
    plain, tech = parts.split("if (!techOn) return out;", 1)
    assert 'btn("tui-open", "Open in Terminal", "is-ghost", `The same ${name}, in a terminal window' in plain
    assert 'btn("tui-install", "Install for Terminal", "is-quiet"' in plain
    for tech_only in ("Terminal version: needs the Rust toolchain", "Terminal version, on the other computer", "t.install_command", "t.signin_command"):
        assert tech_only in tech and tech_only not in plain, tech_only
    assert "Also runs in your terminal" not in CONSOLE_UI_JS
    assert "const row = primary + tui.button;" in CONSOLE_UI_JS
