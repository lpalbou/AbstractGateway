"""HTTPS on this machine's Tailscale name, for exactly as long as the gateway runs.

Browsers offer the microphone, the camera and the clipboard only on https pages (or on the
gateway's own computer), so a phone on the tailnet needs `https://<host>.<tailnet>.ts.net`.
Operator rulings (2026-10-10): the gateway does this itself, ON by default, and the https
address exists only while the gateway is up.

How: `serve` starts `tailscale serve http://127.0.0.1:<port>` in the FOREGROUND (never
`--bg`, which persists in tailscaled after the gateway stops). Tailscale removes a
foreground configuration the moment that command exits, so the address lives exactly as
long as the command. The command runs under a small watchdog (`python -m
abstractgateway.tailscale_https --parent-pid <gateway pid> -- <command>`) that ends it
when the gateway is gone, however the gateway ended: a clean stop terminates the
watchdog; a crash or the event-loop watchdog's `os._exit` leaves it orphaned, and it
notices within a second.

Not only at launch: while the gateway runs, a light loop re-checks every
`RECHECK_S` seconds whenever the address is not active, so Tailscale coming up
later (or its HTTPS certificates being enabled, or a `tailscale serve --bg` being
reset) is picked up without a restart, and a command that ended is started again.

Never taken over: when Tailscale already serves something on this name's port 443 (a
`tailscale serve --bg` or `tailscale funnel --bg` someone ran, another gateway), the gateway
leaves it alone and says so. Off with `serve --tailscale-https off`.

States (`status()`, served in `GET /api/gateway/network` as `tailscale_https`):

    off          --tailscale-https off
    unavailable  no tailscale CLI, Tailscale not running, or HTTPS certificates not enabled
    taken        port 443 of this name is already served by another configuration
    starting     the command was started, Tailscale has not confirmed it yet
    active       https://<name> proxies to this gateway
    failed       the command ended or never took (its output is in the log)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

STATES = ("off", "unavailable", "taken", "starting", "active", "failed")
CONFIRM_TIMEOUT_S = 10.0
RECHECK_S = 30.0
LOG_NAME = "tailscale-https.log"


def _tailscale_binary() -> Optional[str]:
    """The `tailscale` CLI (network_exposure's lookup). One seam, so a test suite can make
    sure no test ever runs the real `tailscale serve` (it changes the machine's tailnet)."""
    from .network_exposure import tailscale_binary

    return tailscale_binary()


def _run_json(cmd: List[str], timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """A CLI's JSON answer, or None when it cannot be run or parsed."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        value = json.loads(out.stdout or "null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _status(state: str, reason: str = "", url: Optional[str] = None, target: Optional[str] = None) -> Dict[str, Any]:
    return {"state": state, "url": url, "target": target, "reason": reason}


def proxy_target(bind_host: str, port: int) -> str:
    """What Tailscale forwards to: loopback for a wildcard or loopback bind (the gateway
    listens there in every network mode), else the one address the gateway is bound to."""
    host = str(bind_host or "").strip().strip("[]")
    if host in ("", "0.0.0.0", "::", "127.0.0.1", "localhost", "::1"):
        return f"http://127.0.0.1:{int(port)}"
    return f"http://[{host}]:{int(port)}" if ":" in host else f"http://{host}:{int(port)}"


def _served_target(serve_config: Dict[str, Any], name: str) -> Optional[str]:
    """The proxy target Tailscale serves at https://<name>/ (any handler on <name>:443
    counts as taken), else None. `serve status --json` lists a background (`--bg`)
    configuration at the top level and each running foreground command's under
    `Foreground` (keyed by its session): both count."""
    configs = [serve_config]
    foreground = serve_config.get("Foreground")
    if isinstance(foreground, dict):
        configs.extend(c for c in foreground.values() if isinstance(c, dict))
    for config in configs:
        found = _served_target_in(config, name)
        if found is not None:
            return found
    return None


def _funnel_on(serve_config: Dict[str, Any], name: str) -> bool:
    """Tailscale Funnel publishes <name>:443 to the internet (`AllowFunnel`), in a background
    configuration or a running foreground command's."""
    configs = [serve_config]
    foreground = serve_config.get("Foreground")
    if isinstance(foreground, dict):
        configs.extend(c for c in foreground.values() if isinstance(c, dict))
    for config in configs:
        allowed = config.get("AllowFunnel")
        if isinstance(allowed, dict) and allowed.get(f"{name}:443") is True:
            return True
    return False


def _served_target_in(serve_config: Dict[str, Any], name: str) -> Optional[str]:
    web = serve_config.get("Web") if isinstance(serve_config.get("Web"), dict) else {}
    site = web.get(f"{name}:443") if isinstance(web, dict) else None
    if isinstance(site, dict):
        handlers = site.get("Handlers") if isinstance(site.get("Handlers"), dict) else {}
        root = handlers.get("/") if isinstance(handlers, dict) else None
        if isinstance(root, dict) and root.get("Proxy"):
            return str(root["Proxy"])
        return "(another handler)"
    tcp = serve_config.get("TCP") if isinstance(serve_config.get("TCP"), dict) else {}
    if isinstance(tcp, dict) and "443" in tcp:
        return "(a TCP forward)"
    return None


def plan(
    target: str,
    *,
    binary: Optional[str],
    run_json: Callable[[List[str]], Optional[Dict[str, Any]]] = _run_json,
) -> Dict[str, Any]:
    """Whether this gateway can serve https on its Tailscale name now (read-only).
    `starting` means: go ahead and start the command."""
    if not binary:
        return _status(
            "unavailable",
            "Tailscale is not installed on this computer. Install it here and on your phone (tailscale.com/download), "
            "sign both in to the same tailnet, and the gateway serves https on its Tailscale name by itself.",
        )
    status = run_json([binary, "status", "--json"])
    if not status or status.get("BackendState") != "Running":
        return _status("unavailable", "Tailscale is installed but not running on this computer: start it and sign in.")
    me = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    name = str(me.get("DNSName") or "").strip().rstrip(".").lower()
    if not name:
        return _status(
            "unavailable", "This computer has no Tailscale name: turn on MagicDNS (Tailscale admin console → DNS)."
        )
    url = f"https://{name}"
    cert_domains = [str(d).strip().rstrip(".").lower() for d in (status.get("CertDomains") or [])]
    if name not in cert_domains:
        return _status(
            "unavailable",
            "HTTPS certificates are not enabled for this tailnet: turn them on (Tailscale admin console → DNS → HTTPS "
            "Certificates); the gateway picks it up within 30 seconds.",
            url=url,
        )
    config = run_json([binary, "serve", "status", "--json"]) or {}
    served = _served_target(config, name)
    if served is not None:
        if served == target and _funnel_on(config, name):
            # The person's own `tailscale funnel --bg` to this gateway: it reaches beyond the
            # tailnet, so no advice to reset it (that would end the public address).
            reason = (
                f"{url} already reaches this gateway through Tailscale Funnel (open to the internet), set up on this "
                "computer; the gateway leaves it alone."
            )
        elif served == target:
            reason = (
                f"{url} is already served to this gateway by a background `tailscale serve`, which stays after the "
                "gateway stops. Run `tailscale serve reset` to let the gateway serve it only while it runs."
            )
        else:
            reason = f"{url} is already served by another configuration ({served}); the gateway leaves it alone."
        return _status("taken", reason, url=url, target=served)
    return _status("starting", "", url=url, target=target)


class TailscaleHttps:
    """The one `tailscale serve` child of this gateway process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._status: Dict[str, Any] = _status("off", "Not started.")

    def status(self) -> Dict[str, Any]:
        with self._lock:
            proc, current = self._proc, dict(self._status)
        if proc is not None and current["state"] in ("starting", "active") and proc.poll() is not None:
            current = _status("failed", f"`tailscale serve` ended (exit {proc.returncode}); see {LOG_NAME}.", url=current["url"])
            with self._lock:
                self._status = dict(current)
        return current

    def disable(self, reason: str = "Turned off for this run (serve --tailscale-https off).") -> Dict[str, Any]:
        with self._lock:
            self._status = _status("off", reason)
            return dict(self._status)

    def start(
        self,
        *,
        bind_host: str,
        port: int,
        log_dir: Path,
        binary: Optional[str] = None,
        run_json: Callable[[List[str]], Optional[Dict[str, Any]]] = _run_json,
        confirm_timeout_s: float = CONFIRM_TIMEOUT_S,
    ) -> Dict[str, Any]:
        if binary is None:
            binary = _tailscale_binary()
        target = proxy_target(bind_host, port)
        decided = plan(target, binary=binary, run_json=run_json)
        if decided["state"] != "starting":
            with self._lock:
                self._status = decided
            return dict(decided)
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / LOG_NAME, "ab")
        cmd = [sys.executable, "-m", "abstractgateway.tailscale_https", "--parent-pid", str(os.getpid()), "--", str(binary), "serve", target]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        except OSError as exc:
            log.close()
            failed = _status("failed", f"could not start `tailscale serve`: {exc}", url=decided["url"])
            with self._lock:
                self._status = failed
            return dict(failed)
        log.close()  # the child holds its own handle
        with self._lock:
            self._proc = proc
            self._status = dict(decided)
        # Active once Tailscale serves the name to this target.
        name = str(decided["url"]).split("://", 1)[1]
        deadline = time.monotonic() + confirm_timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if _served_target(run_json([str(binary), "serve", "status", "--json"]) or {}, name) == target:
                with self._lock:
                    self._status = _status("active", "", url=decided["url"], target=target)
                return self.status()
            time.sleep(0.25)
        if proc.poll() is None:
            self.stop()
        failed = _status("failed", f"Tailscale did not serve {decided['url']}; see {LOG_NAME}.", url=decided["url"])
        with self._lock:
            self._status = failed
        return dict(failed)

    def keep_serving(
        self,
        *,
        bind_host: str,
        port: int,
        log_dir: Path,
        stop_event: threading.Event,
        recheck_s: float = RECHECK_S,
        **start_kwargs: Any,
    ) -> None:
        """Start now, then re-check every `recheck_s` while the address is not active (Tailscale
        down or not set up yet, another configuration on 443, a command that ended), until
        `stop_event` is set. Runs on its own thread; `stop()` ends the command."""
        while not stop_event.is_set():
            if self.status()["state"] != "active":
                self.stop()
                self.start(bind_host=bind_host, port=port, log_dir=log_dir, **start_kwargs)
                if stop_event.is_set():  # the gateway stopped while the command was starting
                    self.stop()
                    return
            stop_event.wait(recheck_s)

    def stop(self) -> None:
        """End the command: Tailscale removes the address with it."""
        with self._lock:
            proc, self._proc = self._proc, None
            if self._status["state"] in ("starting", "active"):
                self._status = _status("off", "The gateway stopped.")
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()  # the watchdog ends the command's whole group, waiting up to 5 s
        try:
            proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


_SINGLETON = TailscaleHttps()


def get_tailscale_https() -> TailscaleHttps:
    return _SINGLETON


def _parent_alive(pid: int) -> bool:
    """The gateway is alive: still our parent (POSIX re-parents an orphan), and running."""
    if os.name != "nt" and os.getppid() != pid:
        return False
    from .tray.platform import pid_alive

    return pid_alive(pid)


def watch(parent_pid: int, cmd: List[str], *, poll_s: float = 1.0) -> int:
    """Run `cmd` while `parent_pid` lives; end it when the parent is gone or on SIGTERM."""
    import signal

    # Its own process group, ended as a group: the `tailscale` on PATH can be a wrapper
    # script that runs the real CLI without `exec` (macOS's /usr/local/bin/tailscale),
    # and ending only the script would leave the real command — and the address — up.
    posix = os.name != "nt"
    child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=posix)

    def _signal_group(sig: int) -> None:
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _end(*_: Any) -> None:
        if posix:
            _signal_group(signal.SIGTERM)
        elif child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if posix:
                _signal_group(signal.SIGKILL)
            else:
                child.kill()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _end)
    signal.signal(signal.SIGINT, _end)
    while child.poll() is None:
        if not _parent_alive(parent_pid):
            _end()
        time.sleep(poll_s)
    return int(child.returncode or 0)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[0] != "--parent-pid" or args[2] != "--":
        print("usage: python -m abstractgateway.tailscale_https --parent-pid PID -- COMMAND...", file=sys.stderr)
        return 2
    return watch(int(args[1]), args[3:])


if __name__ == "__main__":
    raise SystemExit(main())
