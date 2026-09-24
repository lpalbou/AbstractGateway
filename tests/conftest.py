from __future__ import annotations

from pathlib import Path
import sys

import pytest

# ---------------------------------------------------------------------------
# Tests never touch your home or the network.
#
# Why (two incidents, 2026-09-24): an unisolated test downloaded a 1.3 GB
# vision model into ~/.abstractcore/models and rewrote the operator's
# abstractcore.json; a later run rewrote the operator's embedding caches under
# ~/.abstractcore/embeddings as EMPTY files. Isolating one config-file env var
# is not enough: many locations derive from the home directory itself
# (Path.home(), expanduser, huggingface_hub's IMPORT-TIME constants). So HOME
# itself moves -- once here, at conftest import, before any package or
# huggingface_hub is imported, and again for every test.
#
# The same block installs a socket guard: no test reaches a non-loopback host,
# nor the operator's live loopback services (gateway 8080, LM Studio 1234,
# Ollama 11434, the hermetic mission gateway 18850). Any other loopback port
# (TestClient, fake servers on scratch ports) stays allowed.
#
# And a subprocess guard (mission FF): a real `lms` / `ollama` CLI, or the
# desktop `open` / `xdg-open`, spawned by a test escapes the socket guard (the
# child process has its own sockets and talks to the live LM Studio / Ollama,
# or opens a window on the operator's desktop). Launching one is refused,
# recorded, and fails the test. A test's own fake CLI is allowed once it is
# registered with the `fake_cli` fixture.
#
# Opt-outs, each with a MANDATORY reason (collection refuses a bare marker):
#   @pytest.mark.network("reason")    the test genuinely needs the network
#   @pytest.mark.desktop("reason")    the test drives the real desktop / engine CLIs
#   @pytest.mark.real_home("reason")  the test reads the operator's real home
# ---------------------------------------------------------------------------
import ipaddress as _ipaddress
import os
import sys
import re as _re
import shutil as _shutil
import socket as _socket
import tempfile as _tempfile

_REAL_HOME = os.path.expanduser("~")
_SESSION_HOME = Path(_tempfile.mkdtemp(prefix="abstractgateway-tests-home-"))

# Operator-exported path knobs that would steer a test at real data
# (ABSTRACTGATEWAY_FLOWS_DIR, HF_HUB_CACHE, ABSTRACTCORE_JOBS_DIR, ...).
_PATH_KNOB = _re.compile(
    r"^(ABSTRACT[A-Z0-9_]*|HF_[A-Z0-9_]*|HUGGINGFACE_[A-Z0-9_]*)_(DIR|DIRS|PATH|FILE|ROOT|ROOTS|HOME|CACHE|REGISTRY)$"
)
# XDG_* are REMOVED rather than set: every XDG default derives from HOME
# (already tmp), and tests that pass their own fake home expect the platform
# default layout under it, which an exported XDG_CONFIG_HOME would override.
_EXTRA_KNOBS = frozenset(
    {
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    }
)
_OWN_KNOBS = frozenset({"ABSTRACT_TEST_REAL_HOME"})


def _operator_path_knobs() -> list:
    return [k for k in os.environ if k not in _OWN_KNOBS and (_PATH_KNOB.match(k) or k in _EXTRA_KNOBS)]


def _hermetic_env(home: Path) -> dict:
    """Every location the packages derive from the home directory, under `home`."""
    env = {
        "HOME": str(home),
        "HF_HOME": str(home / ".cache" / "huggingface"),
        "HF_HUB_CACHE": str(home / ".cache" / "huggingface" / "hub"),
    }
    # ABSTRACTCORE_CONFIG_DIR is scrubbed (a path knob), not set: the config
    # dir then derives from the tmp HOME, which is also what tests that move
    # HOME themselves expect.
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    return env


for _knob in _operator_path_knobs():
    os.environ.pop(_knob, None)
os.environ.update(_hermetic_env(_SESSION_HOME))
# Read-only pointer for a test that declares @pytest.mark.real_home (e.g. to
# READ installed tokenizers at collection); HOME itself stays tmp.
os.environ["ABSTRACT_TEST_REAL_HOME"] = _REAL_HOME


def _under(path: Path, root: str) -> bool:
    try:
        path.resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def _assert_hf_constants_isolated() -> None:
    """huggingface_hub freezes its cache path at import; fail loudly if it froze on the real home."""
    consts = sys.modules.get("huggingface_hub.constants")
    if consts is None:
        return
    cache = Path(str(getattr(consts, "HF_HUB_CACHE", "")))
    if _under(cache, _REAL_HOME) and not _under(cache, str(_SESSION_HOME)) and not _under(cache, _tempfile.gettempdir()):
        pytest.fail(
            f"huggingface_hub.constants.HF_HUB_CACHE={cache} points into the real home: huggingface_hub "
            "was imported before the test conftest isolated HOME (a plugin or sitecustomize imports it early).",
            pytrace=False,
        )


# -- network guard ----------------------------------------------------------
# 3000-3007: the operator's browser apps (scripts/start-local.sh stack map
# 3001-3005, older launcher defaults 3000/3007). The apps manager probes those
# ports for apps started outside the gateway (mission HH); a test that has not
# replaced the probe finds nothing there instead of the operator's live apps.
_LIVE_LOOPBACK_PORTS = frozenset({8080, 1234, 11434, 18850, 3000, 3001, 3002, 3003, 3004, 3005, 3006, 3007})


class NetworkGuardError(ConnectionRefusedError):
    """A test tried to open a connection the network guard refuses."""


class NetworkGuardResolveError(_socket.gaierror):
    """A test tried to resolve a non-loopback host name."""


_GUARD = {"test": None, "allowed": False, "hits": []}
_GUARD_ALL_HITS: list = []

_REAL_CONNECT = _socket.socket.connect
_REAL_CONNECT_EX = _socket.socket.connect_ex
_REAL_CREATE_CONNECTION = _socket.create_connection
_REAL_GETADDRINFO = _socket.getaddrinfo


def _is_loopback_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    text = str(host).strip().strip("[]").split("%", 1)[0].lower()
    if text in ("", "localhost", "localhost.localdomain") or text.endswith(".localhost"):
        return True
    try:
        ip = _ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_loopback or ip.is_unspecified)


def _is_this_machine_name(host) -> bool:
    """This machine's own host name (and its Bonjour `<name>.local`) resolves to itself."""
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    text = str(host or "").strip().lower().rstrip(".")
    try:
        own = _socket.gethostname().strip().lower().rstrip(".")
    except OSError:
        return False
    short = own[: -len(".local")] if own.endswith(".local") else own.split(".", 1)[0]
    return bool(own) and text in {own, short, f"{short}.local"}


def _is_ip_literal(host) -> bool:
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    try:
        _ipaddress.ip_address(str(host or "").strip().strip("[]").split("%", 1)[0])
        return True
    except ValueError:
        return False


def _port_int(port):
    try:
        return int(port)
    except (TypeError, ValueError):
        return None


def _guard_refuse(api: str, host, port, why: str, exc_type) -> None:
    where = f"{host}:{port}"
    test = _GUARD["test"] or "<collection/session>"
    hit = {"test": test, "api": api, "target": where, "why": why}
    _GUARD["hits"].append(hit)
    _GUARD_ALL_HITS.append(hit)
    raise exc_type(
        f"network guard: {test} tried to reach {where} via {api} ({why}). Point the test at a fake or a "
        "scratch loopback port, or mark it @pytest.mark.network(\"reason\") if it genuinely needs the network."
    )


def _guard_connect_target(api: str, host, port) -> None:
    if _GUARD["allowed"]:
        return
    if not _is_loopback_host(host):
        _guard_refuse(api, host, port, "non-loopback destination", NetworkGuardError)
    if _port_int(port) in _LIVE_LOOPBACK_PORTS:
        _guard_refuse(api, host, port, "the operator's live loopback service", NetworkGuardError)


def _inet_target(sock, address):
    # A datagram connect sends no packet (it only asks the kernel for a route;
    # the gateway's LAN-address probe relies on that), so only stream
    # sockets are guarded here.
    if sock.type != _socket.SOCK_STREAM:
        return None
    if sock.family in (_socket.AF_INET, _socket.AF_INET6) and isinstance(address, tuple) and len(address) >= 2:
        return address[0], address[1]
    return None


def _guarded_connect(self, address):
    target = _inet_target(self, address)
    if target is not None:
        _guard_connect_target("socket.connect", *target)
    return _REAL_CONNECT(self, address)


def _guarded_connect_ex(self, address):
    target = _inet_target(self, address)
    if target is not None:
        _guard_connect_target("socket.connect_ex", *target)
    return _REAL_CONNECT_EX(self, address)


def _guarded_create_connection(address, *args, **kwargs):
    if isinstance(address, tuple) and len(address) >= 2:
        _guard_connect_target("socket.create_connection", address[0], address[1])
    return _REAL_CREATE_CONNECTION(address, *args, **kwargs)


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    # Resolving a loopback name, an IP literal or this machine's own name never
    # leaves the machine; the connect that follows is where a non-loopback
    # address or a live loopback port is refused.
    if (
        not _GUARD["allowed"]
        and not _is_loopback_host(host)
        and not _is_ip_literal(host)
        and not _is_this_machine_name(host)
    ):
        _guard_refuse("socket.getaddrinfo", host, port, "non-loopback name resolution", NetworkGuardResolveError)
    return _REAL_GETADDRINFO(host, port, *args, **kwargs)


_socket.socket.connect = _guarded_connect
_socket.socket.connect_ex = _guarded_connect_ex
_socket.create_connection = _guarded_create_connection
_socket.getaddrinfo = _guarded_getaddrinfo


# -- subprocess guard (mission FF) -------------------------------------------
import shlex as _shlex
import subprocess as _subprocess

# Executables whose real binary reaches past the socket guard: the engine CLIs
# (they talk to the live LM Studio / Ollama servers, load models, start
# daemons) and the desktop openers (a browser tab or an app on the operator's
# screen).
_GUARDED_EXECUTABLES = frozenset({"lms", "ollama", "open", "xdg-open"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_WRAPPERS = frozenset({"exec", "command", "env", "nohup", "sudo", "time", "nice", "caffeinate"})
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", ";;", "|&"}) | _WRAPPERS


class SubprocessGuardError(PermissionError):
    """A test tried to launch a real engine CLI or desktop opener."""


_SUBPROC_GUARD = {"test": None, "allowed": False, "fakes": set(), "hits": []}
_SUBPROC_ALL_HITS: list = []
_REAL_POPEN_INIT = _subprocess.Popen.__init__
_REAL_OS_SYSTEM = os.system


def _guarded_name(program) -> str:
    if isinstance(program, (bytes, bytearray)):
        program = bytes(program).decode("utf-8", "replace")
    name = os.path.basename(str(program or "").strip())
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name if name in _GUARDED_EXECUTABLES else ""


def _shell_programs(command: str) -> list:
    """Program words of a shell command line (first word, and every word after a separator)."""
    try:
        lexer = _shlex.shlex(str(command), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = str(command).split()
    out, at_start = [], True
    for word in words:
        if word in _SHELL_SEPARATORS:
            at_start = True
            continue
        if at_start and "=" in word and not word.startswith(("/", ".")):
            continue  # VAR=value prefix
        if at_start:
            out.append(word)
        at_start = False
    return out


def _programs_of(args, executable=None, shell=False) -> list:
    if shell:
        text = args if isinstance(args, (str, bytes)) else " ".join(str(a) for a in args)
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return _shell_programs(text)
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = [args]
    else:
        argv = list(args or [])
    progs = [executable] if executable else []
    if argv:
        progs.append(argv[0])
        # `env FOO=1 lms ...` / `nohup ollama serve`: the wrapped program.
        if os.path.basename(str(argv[0])) in _WRAPPERS:
            for a in argv[1:]:
                if isinstance(a, str) and not a.startswith("-") and "=" not in a:
                    progs.append(a)
                    break
        # `sh -c "lms ..."` / `bash -lc "..."`: look inside the command string.
        if _guarded_name(argv[0]) == "" and os.path.basename(str(argv[0])) in _SHELLS:
            for i, a in enumerate(argv[1:-1], start=1):
                if isinstance(a, str) and a.startswith("-") and "c" in a:
                    progs.extend(_shell_programs(argv[i + 1]))
                    break
    return progs


def _resolved(program, env) -> str:
    text = os.fspath(program) if isinstance(program, os.PathLike) else str(program)
    if os.sep in text:
        return os.path.realpath(text)
    path = (env or {}).get("PATH") if env is not None else None
    found = _shutil.which(text, path=path)
    return os.path.realpath(found) if found else text


def _subprocess_check(api: str, programs, env=None) -> None:
    if _SUBPROC_GUARD["allowed"]:
        return
    for program in programs:
        name = _guarded_name(program)
        if not name:
            continue
        where = _resolved(program, env)
        if where in _SUBPROC_GUARD["fakes"]:
            continue
        test = _SUBPROC_GUARD["test"] or "<collection/session>"
        hit = {"test": test, "api": api, "program": name, "resolved": where}
        _SUBPROC_GUARD["hits"].append(hit)
        if _SUBPROC_GUARD["test"] is None:  # collection/session: no teardown will report it
            _SUBPROC_ALL_HITS.append(hit)
        raise SubprocessGuardError(
            f"subprocess guard: {test} tried to launch `{name}` ({where}) via {api}. A real engine CLI or "
            "desktop opener escapes the network guard. Fake it (record the argv, or register a stand-in "
            "script with the `fake_cli` fixture), or mark the test @pytest.mark.desktop(\"reason\")."
        )


def _guarded_popen_init(self, args, *pargs, **kwargs):
    # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn, close_fds, shell, ...)
    executable = kwargs.get("executable", pargs[1] if len(pargs) > 1 else None)
    shell = kwargs.get("shell", pargs[7] if len(pargs) > 7 else False)
    _subprocess_check("subprocess.Popen", _programs_of(args, executable, bool(shell)), kwargs.get("env"))
    return _REAL_POPEN_INIT(self, args, *pargs, **kwargs)


def _guarded_os_system(command):
    _subprocess_check("os.system", _shell_programs(command if isinstance(command, str) else os.fsdecode(command)))
    return _REAL_OS_SYSTEM(command)


# subprocess.run / call / check_output and asyncio.create_subprocess_* all
# construct a Popen, so patching its constructor covers them.
_subprocess.Popen.__init__ = _guarded_popen_init
os.system = _guarded_os_system


def _marker_reason(item, name: str):
    """(present, reason) for an opt-out marker; reason must be a non-empty string."""
    marker = item.get_closest_marker(name)
    if marker is None:
        return False, None
    reason = marker.args[0] if marker.args else marker.kwargs.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.{name} needs a reason string, e.g. "
            f"@pytest.mark.{name}(\"why this test needs it\")"
        )
    return True, reason


def _register_hermetic_markers(config) -> None:
    # Collection runs before any test: module-level probes are refused too,
    # unless the whole run opted in with --allow-network.
    _GUARD["allowed"] = bool(config.getoption("--allow-network", default=False))
    config.addinivalue_line(
        "markers",
        "network(reason): the test genuinely needs the network (Hub lookup, real download, live-provider "
        "probe). The reason is mandatory. Without the marker the network guard refuses non-loopback "
        "destinations and the operator's live loopback ports 8080, 1234, 11434 and 18850; with it the test "
        "is skipped unless pytest runs with --allow-network.",
    )
    _SUBPROC_GUARD["allowed"] = bool(config.getoption("--allow-desktop", default=False))
    config.addinivalue_line(
        "markers",
        "desktop(reason): the test launches a real engine CLI (lms, ollama) or desktop opener (open, xdg-open). "
        "The reason is mandatory. Without the marker (or `network`) the subprocess guard refuses those "
        "executables unless the test registered its own stand-in with the `fake_cli` fixture; with it the test "
        "is skipped unless pytest runs with --allow-desktop.",
    )
    config.addinivalue_line(
        "markers",
        "real_home(reason): the test reads the operator's real home directory (read-only, through "
        "ABSTRACT_TEST_REAL_HOME; HOME itself stays a tmp directory). The reason is mandatory, and a test "
        "module that reads ABSTRACT_TEST_REAL_HOME without this marker is a collection error.",
    )


def _add_hermetic_options(parser) -> None:
    parser.addoption(
        "--allow-network",
        action="store_true",
        default=False,
        help="also run the tests marked @pytest.mark.network (they reach real hosts or the live local services); "
        "without it they are skipped with their reason",
    )
    parser.addoption(
        "--allow-desktop",
        action="store_true",
        default=False,
        help="also run the tests marked @pytest.mark.desktop (they launch the real lms / ollama CLIs or open "
        "windows on this desktop); without it they are skipped with their reason",
    )


def _validate_hermetic_markers(config, items) -> None:
    allow_network = bool(config.getoption("--allow-network", default=False))
    allow_desktop = bool(config.getoption("--allow-desktop", default=False))
    for item in items:
        needs_network, why = _marker_reason(item, "network")
        if needs_network and not allow_network:
            item.add_marker(pytest.mark.skip(reason=f"needs the network ({why}); run with --allow-network"))
        needs_desktop, why_desktop = _marker_reason(item, "desktop")
        if needs_desktop and not allow_desktop:
            item.add_marker(pytest.mark.skip(reason=f"drives the real desktop ({why_desktop}); run with --allow-desktop"))
        present, _ = _marker_reason(item, "real_home")
        module = getattr(item, "module", None)
        if not present and module is not None and "ABSTRACT_TEST_REAL_HOME" in _module_source(module):
            raise pytest.UsageError(
                f"{item.nodeid}: reads ABSTRACT_TEST_REAL_HOME without @pytest.mark.real_home(\"reason\")"
            )


_SOURCE_CACHE: dict = {}


def _module_source(module) -> str:
    path = getattr(module, "__file__", None)
    if not path:
        return ""
    if path not in _SOURCE_CACHE:
        try:
            _SOURCE_CACHE[path] = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            _SOURCE_CACHE[path] = ""
    return _SOURCE_CACHE[path]


# Port 9 (discard) on loopback: nothing listens there, and it is not a live
# service port, so a connection is refused at once without a guard hit.
_ENGINE_DEFAULT_URLS = {
    "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
    "OLLAMA_BASE_URL": "http://127.0.0.1:9",
    "OLLAMA_HOST": "http://127.0.0.1:9",
}
# Also for the whole session: a gateway runner thread started by one test can
# boot its bundle host after that test's monkeypatch has been undone.
os.environ.update(_ENGINE_DEFAULT_URLS)


@pytest.fixture(autouse=True)
def isolate_home_and_network(request, monkeypatch, tmp_path_factory):
    """Per-test home under tmp, operator path knobs scrubbed, network guard armed."""
    for knob in _operator_path_knobs():
        monkeypatch.delenv(knob, raising=False)
    home = Path(str(tmp_path_factory.mktemp("home")))
    for key, value in _hermetic_env(home).items():
        monkeypatch.setenv(key, value)
    # HOME stays tmp even under @pytest.mark.real_home: such a test READS the
    # operator's home through ABSTRACT_TEST_REAL_HOME and so cannot write there
    # by accident (collection refuses the pointer in a module without the marker).
    monkeypatch.setenv("ABSTRACT_TEST_REAL_HOME", _REAL_HOME)
    # Local engines' DEFAULT addresses point at a closed port. On a host that
    # is not Apple silicon, AbstractCore's recommended text route is LM Studio,
    # so a gateway booted with no provider configured builds an LM Studio
    # client that lists models at localhost:1234: the operator's live LM
    # Studio on a Linux workstation, a guard hit on CI (release 0.4.0 CI,
    # 2026-09-24). A test that needs a local engine sets its own URL (a
    # loopback fake); one that checks the defaults deletes these.
    for key, value in _ENGINE_DEFAULT_URLS.items():
        monkeypatch.setenv(key, value)
    _assert_hf_constants_isolated()

    allowed, _ = _marker_reason(request.node, "network")
    _GUARD.update(test=request.node.nodeid, allowed=allowed, hits=[])
    desktop, _ = _marker_reason(request.node, "desktop")
    _SUBPROC_GUARD.update(test=request.node.nodeid, allowed=bool(allowed or desktop), fakes=set(), hits=[])
    yield home
    hits = list(_GUARD["hits"])
    _GUARD.update(test=None, allowed=False, hits=[])
    spawn_hits = list(_SUBPROC_GUARD["hits"])
    _SUBPROC_GUARD.update(test=None, allowed=False, fakes=set(), hits=[])
    if hits:
        lines = "\n".join(f"  {h['api']} -> {h['target']} ({h['why']})" for h in hits)
        pytest.fail(f"network guard refused {len(hits)} connection attempt(s):\n{lines}", pytrace=False)
    if spawn_hits:
        _SUBPROC_ALL_HITS.extend(spawn_hits)
        lines = "\n".join(f"  {h['api']} -> {h['program']} ({h['resolved']})" for h in spawn_hits)
        pytest.fail(f"subprocess guard refused {len(spawn_hits)} launch(es):\n{lines}", pytrace=False)


@pytest.fixture
def subprocess_guard():
    """The live guard state, for the guard's own tests (they clear `hits` once asserted)."""
    return _SUBPROC_GUARD


@pytest.fixture
def fake_cli(tmp_path):
    """Register a stand-in engine CLI / opener: `fake_cli("lms", "#!/bin/sh\\necho ok\\n")`.

    Writes an executable script named `name` into a per-test bin directory and
    tells the subprocess guard that THIS file (and only it) may run. Returns the
    script path; put its parent on PATH or pass the path explicitly.
    """
    bindir = tmp_path / "fake-cli-bin"

    def make(name: str, script: str = "#!/bin/sh\nexit 0\n") -> Path:
        bindir.mkdir(parents=True, exist_ok=True)
        path = bindir / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
        _SUBPROC_GUARD["fakes"].add(os.path.realpath(str(path)))
        return path

    return make


@pytest.fixture
def fake_public_dns(monkeypatch):
    """Resolve every non-loopback name to one fixed public address, without DNS.

    For code that resolves a host before a faked fetch (the fetch_url SSRF
    check, the server's base-URL allowlist). The answer is 93.184.216.34, a
    public address, so the SSRF rules still see a public destination; a real
    connect to it is still refused by the guard.
    """

    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        if _is_loopback_host(host) or _is_ip_literal(host):
            return _REAL_GETADDRINFO(host, port, family, type, proto, flags)
        return [(_socket.AF_INET, _socket.SOCK_STREAM, _socket.IPPROTO_TCP, "", ("93.184.216.34", _port_int(port) or 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", resolve)
    return resolve


def _hermetic_terminal_summary(terminalreporter) -> None:
    if _SUBPROC_ALL_HITS:
        terminalreporter.section("subprocess guard")
        for hit in _SUBPROC_ALL_HITS:
            terminalreporter.write_line(f"{hit['test']}: {hit['api']} -> {hit['program']} ({hit['resolved']})")
    if not _GUARD_ALL_HITS:
        return
    terminalreporter.section("network guard")
    for hit in _GUARD_ALL_HITS:
        terminalreporter.write_line(f"{hit['test']}: {hit['api']} -> {hit['target']} ({hit['why']})")


def _hermetic_sessionfinish(session) -> None:
    if any(h["test"] == "<collection/session>" for h in _GUARD_ALL_HITS + _SUBPROC_ALL_HITS) and session.exitstatus == 0:
        session.exitstatus = 1


def _hermetic_unconfigure() -> None:
    _shutil.rmtree(_SESSION_HOME, ignore_errors=True)
# ------------------------------------------------------------ end hermetic block



@pytest.fixture(autouse=True)
def _isolate_repo_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prevent accidental writes to a developer’s real repo when running tests in an
    # environment where the gateway is configured for backlog browsing/triage.
    monkeypatch.delenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", raising=False)
    monkeypatch.delenv("ABSTRACT_TRIAGE_REPO_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _isolate_gateway_runtime_env(
    isolate_home_and_network: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # HOME, HF_HOME/HF_HUB_CACHE, XDG_* and the operator's exported path knobs
    # are already isolated by `isolate_home_and_network` (above); this fixture
    # layers the gateway's own data/flows/registry/store locations on top.
    # Prevent accidental writes to a developer’s real gateway DB/runtime dir when running
    # tests in an environment where `agw.sh` (or similar) exported durable paths.
    monkeypatch.delenv("ABSTRACTGATEWAY_DB_PATH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_STORE_BACKEND", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_TOKEN", raising=False)
    # USER_AUTH=1 flips entity homes to per-principal roots — 6 replay tests
    # fail under a launcher shell that exported it (env-poisoning class;
    # adversary finding, 2026-07-17).
    monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
    # `serve` exports these provenance markers into os.environ (first-run,
    # 2026-09-23); a test that ran serve must not leak them into the next.
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_MODE_SOURCE", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_DATA_DIR_SOURCE", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_BIND_HOST", raising=False)
    # AbstractCore's host job registry persists job snapshots under the user's
    # ~/.abstractcore by default; a test that reaches the real registry keeps
    # its jobs in memory instead.
    monkeypatch.setenv("ABSTRACTCORE_JOBS_PERSIST", "0")

    # Provide safe defaults so tests that forget to set these still write only under tmp.
    base = Path(str(tmp_path_factory.mktemp("abstractgateway-test-env")))
    (base / "runtime").mkdir(parents=True, exist_ok=True)
    (base / "flows").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(base / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(base / "flows"))
    # Isolate the MACHINE-LEVEL data registry (~/.abstractframework/
    # data_registry.json): service boot registers data homes
    # (register_gateway_data_homes), so an unisolated suite run pollutes the
    # operator's REAL registry with hundreds of phantom tmp-dir rows (live
    # incident 2026-07-14: 1000+ rows from one evening's suites). Every test
    # writes its own throwaway registry file instead.
    monkeypatch.setenv("ABSTRACTFRAMEWORK_DATA_REGISTRY", str(base / "data_registry.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    # Isolate THE AbstractCore store. Since the operator's one-store ruling
    # (2026-08-01) a Gateway write to a capability default or a provider
    # profile lands in AbstractCore's own config file, so an unisolated suite
    # would edit the developer's real ~/.abstractcore/config/abstractcore.json
    # -- the very store these tests assert about. The path deliberately does
    # NOT exist: "no file at the Core path" is what a fresh install looks like,
    # which is what the seed contract is stated against.
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(base / "abstractcore" / "abstractcore.json"))
    # The config DIR follows the file: code that derives sibling stores from
    # the directory (hub-catalog cache, host jobs) stays inside `base` too.
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_DIR", str(base / "abstractcore"))
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)


@pytest.fixture(autouse=True)
def _reset_gateway_service_between_tests(_isolate_gateway_runtime_env: None):
    from abstractgateway.service import stop_gateway_runner

    stop_gateway_runner()
    sys.modules.pop("abstractgateway.app", None)
    yield
    stop_gateway_runner()
    sys.modules.pop("abstractgateway.app", None)


@pytest.fixture(autouse=True)
def _no_external_app_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The apps manager looks for apps started outside the gateway on the
    usual loopback ports (3001-3005, 3000, 3007): the operator's live apps.
    No port is probed in a test unless the test names its own
    (`detect_external_apps(ports=...)`, or a manager's `external_probe`)."""
    import abstractgateway.apps_manager as _am

    monkeypatch.setattr(_am, "external_probe_ports", lambda: [])


@pytest.fixture(autouse=True)
def _no_real_desktop_app(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """The Assistant card (apps_desktop.py) looks at /Applications, this
    Python's scripts folder, the importable package and the process list: the
    operator's real machine. In a test nothing is found and nothing runs
    unless the test hands a manager its own `desktop_probes` (or calls
    `detect_assistant` with its own DesktopProbes)."""
    import abstractgateway.apps_desktop as _desk

    empty = tmp_path_factory.mktemp("no-desktop-apps")

    def _nothing() -> "_desk.DesktopProbes":
        return _desk.DesktopProbes(
            which=lambda _name: None,
            exists=lambda _path: False,
            find_spec=lambda _name: None,
            home=empty,
            script_dirs=[str(empty)],
            dist_version=lambda _name: None,
            plist_version=lambda _path: None,
            entry_point=lambda _script: None,
            processes=lambda: [],
        )

    monkeypatch.setattr(_desk, "system_probes", _nothing)
    monkeypatch.setattr(_desk, "_process_argvs", lambda: [])


def pytest_configure(config):
    _register_hermetic_markers(config)


def pytest_collection_modifyitems(config, items):
    _validate_hermetic_markers(config, items)


def pytest_addoption(parser):
    _add_hermetic_options(parser)

def pytest_terminal_summary(terminalreporter):
    _hermetic_terminal_summary(terminalreporter)


def pytest_sessionfinish(session, exitstatus):
    _hermetic_sessionfinish(session)


def pytest_unconfigure(config):
    _hermetic_unconfigure()
