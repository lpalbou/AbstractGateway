"""ADR 0003 (engine installation contract), checked for every engine on every OS the gateway runs on.

The hosts are simulated (`HostFacts`), so this file runs on every CI runner (Linux and the Windows
job) and on a Mac: installed under the gateway's service account, no root where the vendor has a
non-root path, prebuilt binaries only (never a compiler), atomic updates.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from abstractgateway import engines_install as ei
from test_gateway_engines_install import FakeSystem, _installer, _registry

HOSTS = {
    "linux-cuda": ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cuda", libc="glibc"),
    "linux-cpu": ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cpu", libc="glibc"),
    "linux-root": ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cpu", libc="glibc", is_root=True),
    "linux-arm": ei.HostFacts(os_id="linux", arch="arm64", libc="glibc"),
    "macos-arm": ei.HostFacts(os_id="darwin", arch="arm64", accelerator="metal", macos_version=(15, 0)),
    "macos-intel": ei.HostFacts(os_id="darwin", arch="x86_64", macos_version=(14, 5)),
    "windows-x64": ei.HostFacts(os_id="windows", arch="x86_64", accelerator="cuda"),
    "windows-arm": ei.HostFacts(os_id="windows", arch="arm64"),
}
ENGINES = ("llamacpp", "ollama", "lmstudio", "vllm", "huggingface", "mlx")
NATIVE = ("llama-cpp-python", "torch")


def _plan(tmp_path: Path, host_id: str, engine: str, **whiches: str) -> ei.InstallPlan:
    sysd = FakeSystem(tmp_path)
    sysd.whiches.update({"zstd": "/usr/bin/zstd", **whiches})
    return _installer(tmp_path, sysd, host=HOSTS[host_id]).plan(engine)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("host_id", sorted(HOSTS))
def test_no_plan_needs_a_compiler_or_a_source_build(tmp_path, host_id, engine) -> None:
    plan = _plan(tmp_path, host_id, engine)
    text = " ".join([plan.notes or "", *map(str, plan.command_preview or []), *map(str, plan.steps or [])]).lower()
    assert "from source" not in text.replace("never builds engines from source", "")
    for needs in ("needs a compiler", "c/c++ compiler", "build-essential", "xcode-select", "command-line tools", "cmake"):
        assert needs not in text, (needs, text[:300])
    if plan.needs_tools:  # the one system prerequisite left: LM Studio's libatomic1 on Linux
        assert engine == "lmstudio" and HOSTS[host_id].os_id == "linux"
    argv = [str(a) for a in (plan.command_preview or [])]
    if plan.available and argv[1:3] == ["pip", "install"]:
        for name in NATIVE:
            assert any(argv[i] == "--only-binary" and argv[i + 1] == name for i in range(len(argv) - 1)), (name, argv)


@pytest.mark.parametrize("host_id", ["linux-cpu", "linux-root", "linux-arm"])
def test_ollama_on_linux_is_the_vendor_archive_under_the_service_account_never_a_root_script(tmp_path, host_id) -> None:
    plan = _plan(tmp_path, host_id, "ollama")
    assert plan.available and plan.method == "user_archive" and not plan.needs_admin
    assert "install.sh" not in " ".join(map(str, plan.command_preview))
    assert "for this account" not in plan.notes
    assert "the gateway's service account" in plan.notes and "shared by every user" in plan.notes


def test_an_ollama_update_that_fails_to_unpack_keeps_the_installed_one(tmp_path) -> None:
    host = HOSTS["linux-cpu"]
    sysd = FakeSystem(tmp_path)
    sysd.whiches.update({"zstd": "/usr/bin/zstd", "systemctl": "/usr/bin/systemctl"})
    home = tmp_path / "home"
    old = home / ".local" / "bin" / "ollama"
    old.parent.mkdir(parents=True)
    old.write_text("old ollama\n")

    def unpack_fails(argv):
        into = Path(shlex.split(argv[2].split("| tar -xf - -C", 1)[1])[0])
        (into / "bin").mkdir(parents=True, exist_ok=True)
        (into / "bin" / "ollama.partial").write_text("half")
        return 2, "tar: Unexpected EOF in archive"

    sysd.on(lambda a: a[:2] == ["sh", "-c"] and "zstd -d -c" in a[2], unpack_fails)
    inst = _installer(tmp_path, sysd, host=host)
    reg = _registry(inst, tmp_path)
    snap, _ = reg.start("ollama", force=True, run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "failed" and job["error"]["code"] == "unpack_failed"
    assert old.read_text() == "old ollama\n", "the installed Ollama was touched by a failed update"
    assert not [p for p in (home / ".local").iterdir() if p.name.startswith(".ollama-staging")]


def test_swap_in_restores_the_old_version_when_the_new_one_cannot_be_moved(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bin" / "ollama"
    target.parent.mkdir()
    target.write_text("old")
    new = tmp_path / "staged-ollama"
    new.write_text("new")
    real = ei.os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 2:  # the new one cannot be moved in
            raise OSError("cross-device link")
        return real(a, b)

    monkeypatch.setattr(ei.os, "replace", flaky)
    with pytest.raises(OSError):
        ei._swap_in(new, target)
    assert target.read_text() == "old"
