"""ADR 0003 (engine installation contract), checked for every engine on every OS the gateway runs on.

The hosts are simulated (`HostFacts`), so this file runs on every CI runner (Linux and the Windows
job) and on a Mac: installed under the gateway's service account, no root where the vendor has a
non-root path, prebuilt binaries only (never a compiler), atomic updates.
"""
from __future__ import annotations

import json
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


UV_NO_WHEEL = (
    "  x No solution found when resolving dependencies:\n"
    "  `-> Because llama-cpp-python==0.3.36 has no wheels with a matching platform tag (e.g., `manylinux_2_35_x86_64`)\n"
    "      and building source distributions is disabled for llama-cpp-python, we can conclude that\n"
    "      abstractcore[gpu]==2.31.1 cannot be used."
)


def test_huggingface_without_a_matching_wheel_fails_readably_instead_of_compiling(tmp_path) -> None:
    """The 8080 incident (2026-10-29): a Hugging Face install pulled AbstractCore's gpu setting, whose
    llama-cpp-python was not installed, and uv took the PyPI sdist and compiled it. Now the install is
    wheels-only and its failure says which wheel is missing, for which host, from which index."""
    host = HOSTS["linux-cpu"]
    sysd = FakeSystem(tmp_path)
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "m.version('llama-cpp-python')" in a[-1], lambda a: (1, "PackageNotFoundError"))
    sysd.on(lambda a: a[:3] == ["/fake/uv", "pip", "install"], lambda a: (1, UV_NO_WHEEL))
    reg = _registry(_installer(tmp_path, sysd, host=host), tmp_path)
    snap, _ = reg.start("huggingface", force=True, run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "failed" and job["error"]["code"] == "no_prebuilt_wheel", job["message"]
    msg = job["message"]
    assert msg.startswith("A prebuilt wheel is not available for this platform (llama-cpp-python for linux x86_64")
    assert "https://abetlen.github.io/llama-cpp-python/whl/cpu/llama-cpp-python/" in msg and "never builds" in msg
    installs = [c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"]]
    assert len(installs) == 1, installs  # no second (source) attempt
    argv = installs[0]
    assert "llama-cpp-python==0.3.36" in argv and ["--only-binary", "llama-cpp-python"] == argv[argv.index("--only-binary"):argv.index("--only-binary") + 2]


def test_an_update_that_fails_midway_puts_the_previous_package_back_and_it_loads(tmp_path) -> None:
    """Atomic update: the new wheel's install fails after replacing a package; the previous version is
    reinstalled, and the load check runs on the restored copy (and passes)."""
    sysd = FakeSystem(tmp_path)
    env = {"llama-cpp-python": "0.3.20", "abstractcore": "2.31.1", "numpy": "2.1.0"}

    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "__AG_PKGS__" in a[-1], lambda a: (0, "__AG_PKGS__" + json.dumps(env)))
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "m.version('llama-cpp-python')" in a[-1] and "__AG" not in a[-1],
            lambda a: (0, env["llama-cpp-python"]))
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "__AG_VERIFY__" in a[-1] and "gpu_offload" not in a[-1],
            lambda a: (0, "__AG_VERIFY__" + json.dumps({"version": env["llama-cpp-python"]})))

    def uv(argv):
        if "--no-deps" in argv and "llama-cpp-python==0.3.20" in argv:  # the restore
            env["llama-cpp-python"] = "0.3.20"
            return 0, "Installed 1 package"
        env["llama-cpp-python"] = "0.3.28"  # midway: the new one is in place...
        return 1, "error: Failed to install: libggml.so: No space left on device"  # ...then the install fails

    sysd.on(lambda a: a[:3] == ["/fake/uv", "pip", "install"], uv)
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("llamacpp", force=True, run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "failed", job["message"]
    assert env["llama-cpp-python"] == "0.3.20", "the previous llama.cpp is not back"
    assert "put back to the versions they had" in job["message"]
    assert "The previous version loads again (0.3.20)." in job["message"]
    restore = [c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"] and "--no-deps" in c and "llama-cpp-python==0.3.20" in c]
    assert restore and ["--only-binary", "llama-cpp-python"] == restore[0][restore[0].index("--only-binary"):restore[0].index("--only-binary") + 2]
    i_restore = sysd.calls.index(restore[0])
    assert any("__AG_VERIFY__" in c[-1] for c in sysd.calls[i_restore + 1:] if c[:1] == ["/gw/bin/python"]), "no load check after the restore"
