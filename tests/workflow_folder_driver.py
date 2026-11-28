"""One gateway START in its own process (not a test module): `python workflow_folder_driver.py SPEC OUT`.

The folder next to the running code (`config._default_flows_dir`, relative to
`abstractgateway/config.py`'s own file) is one of the folders a start can serve, so "the code lives
in tree A / tree B" can only be made real by running the code FROM tree A / tree B. The
test (tests/workflow_folder_fixtures.py) builds each tree with a copy of `src/abstractgateway`
and runs this script with `PYTHONPATH` naming that copy; the first thing done here is to prove
the import came from that copy.

A start is the real `abstractgateway serve` (`cli.main(["serve", ...])`, no tray, no Tailscale,
no keep-awake, models in-process; `--flows-dir` when the SPEC names one), with ONE substitution: uvicorn's Server does not open a
listener; it runs the app in this process through a TestClient (the lifespan boots the service on
its thread) and waits for the boot to settle. A FAILED START is exit code 3 — `serve` ended before
serving (its SystemExit text is on stderr) or the boot did not end `ready` — with every word the
gateway said about it on stderr. Otherwise the script runs the SPEC's operations through the HTTP
API (and reads the registry the service built), writes their results as JSON to OUT and exits 0.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _sha(path: str) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _principal(user: Optional[str]):
    from abstractgateway.security.principal import GatewayPrincipal

    return GatewayPrincipal(user_id=user, roles=("user",)) if user else None


def _service(user: Optional[str]):
    from abstractgateway.service import get_gateway_service, get_gateway_service_for_principal

    return get_gateway_service_for_principal(_principal(user)) if user else get_gateway_service()


def _registry(user: Optional[str]) -> Dict[str, Any]:
    """Every PRIVATE `id@version` the plane's registry resolves, with the sha256 of the file it was
    read from and that file's folder (the tenant catalog is governance, not the registry)."""
    host = _service(user).host
    out: Dict[str, Any] = {}
    for bid, versions in (getattr(host, "bundle_sources", {}) or {}).items():
        for ver, meta in (versions or {}).items():
            if str((meta or {}).get("registry_scope") or "private") != "private":
                continue
            path = str((meta or {}).get("path") or "")
            out[f"{bid}@{ver}"] = {"sha256": _sha(path), "folder": str(Path(path).parent.resolve()) if path else None}
    return out


def _resolves(user: Optional[str], workflow_ids: List[str]) -> Dict[str, bool]:
    reg = _service(user).host.workflow_registry
    return {wid: reg.get(wid) is not None for wid in workflow_ids}


def main(spec_path: str, out_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    import abstractgateway

    imported = Path(abstractgateway.__file__).resolve().parent
    expected = Path(spec["package_dir"]).resolve()
    if imported != expected:
        print(f"DRIVER: abstractgateway was imported from {imported}, not from {expected}", file=sys.stderr)
        return 4

    if spec.get("note_off", True):
        # As tests/conftest.py does: AbstractNote's packs are not this test's subject.
        from abstractgateway import abstractnote_seam

        abstractnote_seam.note_bundle_paths = lambda _root: []  # type: ignore[assignment]

    tokens: Dict[str, str] = dict(spec.get("tokens") or {})
    if spec.get("users"):
        from abstractgateway.users import GatewayUserRegistry

        reg = GatewayUserRegistry()
        for name in spec["users"]:
            if name not in tokens:
                _u, tok = reg.create_user(user_id=name, roles=["user"])
                tokens[name] = tok

    thread_errors: List[str] = []
    real_hook = threading.excepthook

    def _hook(args):  # a SystemExit/BaseException on the boot thread is a failed start too
        thread_errors.append(f"{args.exc_type.__name__}: {args.exc_value}")
        real_hook(args)

    threading.excepthook = _hook

    # The start is the real `abstractgateway serve` (cli.main): everything it does before it hands
    # the app to uvicorn runs as in production. Only the listener is replaced: uvicorn's Server
    # runs the app in-process through a TestClient (lifespan included), then the SPEC's operations.
    import socket

    import uvicorn

    from abstractgateway import cli

    outcome: Dict[str, Any] = {"served": False, "rc": None}

    class _Config:
        def __init__(self, app: str, **kwargs: Any) -> None:
            self.app = app
            self.kwargs = kwargs
            self.timeout_graceful_shutdown = kwargs.get("timeout_graceful_shutdown")

    class _Server:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False
            self.force_exit = False
            self.started = False
            self.servers: list = []

        def handle_exit(self, sig: int, frame: Any) -> None:
            self.should_exit = True

        def run(self, sockets: Any = None) -> None:
            outcome["served"] = True
            self.started = True
            outcome["rc"] = _serve_ops(spec, tokens, out_path, imported, thread_errors)
            self.should_exit = True

    uvicorn.Config = _Config  # type: ignore[assignment]
    uvicorn.Server = _Server  # type: ignore[assignment]
    # As tests/conftest.py does: serve's production belt (os._exit when a non-daemon thread is
    # left after shutdown) would end this process before it reports.
    cli._exit_now_if_nondaemon_stragglers = lambda *a, **k: None  # type: ignore[assignment]

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])  # never bound by the start: no listener runs
    argv = ["serve", "--host", "127.0.0.1", "--port", str(port), "--no-tray", "--tailscale-https", "off",
            "--keep-awake", "off", "--model-host", "in-process", "--watchdog-seconds", "0"]
    if spec.get("flows_dir"):
        argv += ["--flows-dir", str(spec["flows_dir"])]
    exit_code: Any = None
    try:
        cli.main(argv)
    except SystemExit as e:
        exit_code = e.code
    except BaseException as e:  # noqa: BLE001 - a start that raised is a failed start
        exit_code = f"{type(e).__name__}: {e}"
    if not outcome["served"]:
        print(f"START FAILED: `abstractgateway serve` ended before serving; exit: {exit_code}", file=sys.stderr)
        return 3
    return int(outcome["rc"] or 0)


def _serve_ops(spec: Dict[str, Any], tokens: Dict[str, str], out_path: str, imported: Path, thread_errors: List[str]) -> int:
    from fastapi.testclient import TestClient

    import abstractgateway.service as service
    from abstractgateway.app import app

    results: Dict[str, Any] = {"tokens": tokens, "package_dir": str(imported)}
    try:
        client_cm = TestClient(app)
        c = client_cm.__enter__()
    except BaseException as e:  # noqa: BLE001 - the start itself refused
        print(f"START FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    try:
        state = service.wait_for_gateway_boot(timeout_s=float(spec.get("boot_timeout_s", 240)))
        if state != "ready" or thread_errors:
            print(f"START FAILED: boot state {state!r}; error: {getattr(service, '_boot_error', None)}; "
                  f"thread errors: {thread_errors}", file=sys.stderr)
            return 3

        def headers(user: Optional[str]) -> Dict[str, str]:
            return {"Authorization": f"Bearer {tokens[user] if user else spec['admin_token']}"}

        saved: Dict[str, Any] = {}
        for i, op in enumerate(spec.get("ops") or []):
            kind = op["op"]
            user = op.get("user")
            key = op.get("save") or f"{i}:{kind}"
            if kind == "registry":
                saved[key] = _registry(user)
            elif kind == "resolves":
                saved[key] = _resolves(user, list(op["workflow_ids"]))
            elif kind == "create_automation":
                r = c.post("/api/gateway/automations", headers=headers(user), json=op["body"])
                if r.status_code != 200:
                    print(f"DRIVER: create automation answered {r.status_code}: {r.text}", file=sys.stderr)
                    return 5
                saved[key] = r.json()
            elif kind == "run_now":
                aid = op.get("automation_id") or saved[op["automation_from"]]["automation_id"]
                saved[key] = _run_now(c, headers(user), aid, op["command_id"], int(op["index"]))
            else:
                print(f"DRIVER: unknown op {kind!r}", file=sys.stderr)
                return 6
        results["ops"] = saved
    finally:
        try:
            client_cm.__exit__(None, None, None)
        except BaseException as e:  # noqa: BLE001 - the stop is not the subject
            print(f"DRIVER: stop raised {type(e).__name__}: {e}", file=sys.stderr)
    Path(out_path).write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _run_now(c, headers: Dict[str, str], aid: str, command_id: str, index: int) -> Dict[str, Any]:
    r = c.post(f"/api/gateway/automations/{aid}/commands", headers=headers,
               json={"command_id": command_id, "type": "automation.run_now", "payload": {}})
    if r.status_code != 200:
        return {"command_status": r.status_code, "command_body": r.text}
    deadline = time.time() + 120
    summary: Dict[str, Any] = {}
    while time.time() < deadline:
        s = c.get(f"/api/gateway/automations/{aid}", headers=headers)
        summary = s.json().get("summary") or {} if s.status_code == 200 else {}
        last = summary.get("last_occurrence") or {}
        if last.get("index") == index and last.get("status") in {"completed", "failed"}:
            break
        if summary.get("status") in {"failed", "paused"} and (last.get("index") or 0) < index:
            break
        time.sleep(0.1)
    occ = c.get(f"/api/gateway/automations/{aid}/occurrences", headers=headers)
    items = (occ.json().get("items") or []) if occ.status_code == 200 else []
    mine = [o for o in items if o.get("index") == index or o.get("occurrence_index") == index]
    workflow_id = None
    if mine and mine[0].get("run_id"):
        run = c.get(f"/api/gateway/runs/{mine[0]['run_id']}", headers=headers)
        if run.status_code == 200:
            workflow_id = run.json().get("workflow_id")
    return {"summary": summary, "occurrence": mine[0] if mine else None, "workflow_id": workflow_id}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
