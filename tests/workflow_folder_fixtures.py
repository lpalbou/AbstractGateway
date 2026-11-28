"""Code trees, data folders and gateway starts for tests/test_workflow_folder_incident.py (not a test module).

The rule (operator, 2026-11-28): a workflow that exists is used whichever code runs. A start serves
its `--flows-dir`, else the folder its data folder remembers, else the folder next to the running
code (`config._default_flows_dir`, derived from where `abstractgateway/config.py` sits). So a test
that means "the code lives in tree A" must run the code FROM tree A:

- `checkout_tree(root)`: a source checkout — `src/abstractgateway` (a copy of this repository's
  package), `pyproject.toml`, `.gitignore` and `flows/bundles` holding the files the repository
  ships (the tracked shape a fresh copy of the repository has), plus any extra files given;
- `start(tree, data_dir, ...)`: one `abstractgateway serve` in a subprocess whose `PYTHONPATH` names
  the tree (tests/workflow_folder_driver.py; it refuses to run if the import came from elsewhere).

Hermetic: tmp trees and data folders, the suite's tmp HOME, no network, no model, no port.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO = Path(__file__).resolve().parents[1]
# The package copied into every tree: this repository's, unless WORKFLOW_FOLDER_TESTS_PACKAGE_SRC
# names another copy (a scratch copy with one rule broken, to prove a test goes red for it).
PACKAGE_SRC = Path(os.environ.get("WORKFLOW_FOLDER_TESTS_PACKAGE_SRC") or (REPO / "src" / "abstractgateway")).resolve()
REPO_BUNDLES = REPO / "flows" / "bundles"
DRIVER = Path(__file__).resolve().parent / "workflow_folder_driver.py"
ADMIN_TOKEN = "t"
#: The retired flows-dir variables: nothing reads them any more (a spawned start must ignore them).
RETIRED_FLOWS_VARS = ("ABSTRACTGATEWAY_FLOWS_DIR", "ABSTRACTFRAMEWORK_WORKFLOWS_DIR", "ABSTRACTFLOW_FLOWS_DIR")

PUBLISHED_ID = "demo-flow"
PUBLISHED_VERSION = "0.0.7"
PUBLISHED_REF = f"{PUBLISHED_ID}@{PUBLISHED_VERSION}"
PUBLISHED_FLOW = "echo"
PUBLISHED_WORKFLOW = f"{PUBLISHED_REF}:{PUBLISHED_FLOW}"


def repo_shipped_names() -> List[str]:
    """The `.flow` files this repository ships in `flows/bundles` (the `.gitignore` exceptions and
    the wheel force-include list), read with the rule `workflow_sources.checkout_shipped_names`."""
    from abstractgateway.workflow_sources import checkout_shipped_names

    names = sorted(checkout_shipped_names(REPO))
    assert "basic-agent.flow" in names, names
    return names


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_published_bundle(folder: Path, *, bundle_id: str = PUBLISHED_ID, version: str = PUBLISHED_VERSION) -> Path:
    """A bundle as the gateway's publish route writes it (`<id>@<version>.flow`, the publish stamp),
    whose entry flow answers deterministically (the automations echo flow: no model, no tool)."""
    from automations_fixtures import echo_flow

    folder.mkdir(parents=True, exist_ok=True)
    flow = echo_flow()
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": version,
        "created_at": "2026-11-01T00:00:00+00:00",
        "default_entrypoint": flow["id"],
        "entrypoints": [{"flow_id": flow["id"], "name": flow["id"], "description": "", "interfaces": []}],
        "flows": {flow["id"]: f"flows/{flow['id']}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {
            "publisher": {"host": "abstractgateway", "user_id": "admin"},
            "lifecycle": {"source": "abstractflow.editor", "published_at": "2026-11-01T00:00:00+00:00"},
        },
    }
    path = folder / f"{bundle_id}@{version}.flow"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow['id']}.json", json.dumps(flow, indent=2))
    return path


def marker_folder(path: Path, *markers: str) -> Path:
    """A workflow folder of its own: the shipped basic-agent plus one published marker bundle per
    name (`<name>@1.0.0`), so the registry says whether this folder is the one served."""
    path = Path(path)
    path.mkdir(parents=True)
    shutil.copy2(REPO_BUNDLES / "basic-agent.flow", path / "basic-agent.flow")
    for name in markers:
        write_published_bundle(path, bundle_id=name, version="1.0.0")
    return path.resolve()


def flow_files(root: Path) -> Dict[str, str]:
    """Every `.flow` under `root` (relative path → sha256)."""
    root = Path(root)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): sha256(p) for p in sorted(root.rglob("*.flow")) if p.is_file()}


def data_dir_flow_files(data_dir: Path) -> Dict[str, str]:
    """The `.flow` files in a data folder outside the tenant catalog (`workflow_catalog/`: the boot
    publishes the console's shipped transport bundle there, by design)."""
    return {k: v for k, v in flow_files(data_dir).items() if not k.startswith("workflow_catalog/")}


@dataclass
class Tree:
    """Code the gateway can run from."""

    root: Path
    pythonpath: Path  # what PYTHONPATH names
    package_dir: Path  # <...>/abstractgateway (where config.py lives)
    code_folder: Path  # the code's own flows/bundles


def checkout_tree(root: Path, *, extra: Iterable[Path] = ()) -> Tree:
    """A source checkout (the layout `_default_flows_dir` recognises as `<package>/../../flows/bundles`).
    The .pyc caches are copied too (copy2 keeps the source mtimes they are keyed on): a cold
    compile of the whole package in every tree is the slowest part of a start otherwise."""
    root = Path(root)
    src = root / "src"
    shutil.copytree(PACKAGE_SRC, src / "abstractgateway")
    for name in ("pyproject.toml", ".gitignore"):
        shutil.copy2(REPO / name, root / name)
    folder = root / "flows" / "bundles"
    folder.mkdir(parents=True)
    for name in repo_shipped_names():
        shutil.copy2(REPO_BUNDLES / name, folder / name)
    for p in extra:
        shutil.copy2(p, folder / Path(p).name)
    return Tree(root=root, pythonpath=src, package_dir=src / "abstractgateway", code_folder=folder)


@dataclass
class Start:
    returncode: int
    stdout: str
    stderr: str
    results: Dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def ops(self) -> Dict[str, Any]:
        return self.results.get("ops") or {}

    def why(self) -> str:
        return f"exit {self.returncode}\n--- stderr (tail) ---\n{self.stderr[-6000:]}"


def gateway_env(tree: Tree, data_dir: Path, *, user_auth: bool = False, runner: bool = True,
                extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The start's environment: the suite's hermetic one (tmp HOME, scrubbed path knobs), the code on
    PYTHONPATH, the data folder; `extra_env` last."""
    env = dict(os.environ)
    for name in RETIRED_FLOWS_VARS:
        env.pop(name, None)
    env.pop("PYTHONHOME", None)
    env.update({
        "PYTHONPATH": str(tree.pythonpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ABSTRACTGATEWAY_DATA_DIR": str(data_dir),
        "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle",
        "ABSTRACTGATEWAY_AUTH_TOKEN": ADMIN_TOKEN,
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": "*",
        "ABSTRACTGATEWAY_POLL_S": "0.05",
        "ABSTRACTGATEWAY_TICK_WORKERS": "1",
        "ABSTRACTGATEWAY_RUNNER": "1" if runner else "0",
        "ABSTRACTGATEWAY_ENTITY_SELF_REPAIR": "0",
        "ABSTRACTCORE_JOBS_PERSIST": "0",
    })
    if user_auth:
        env["ABSTRACTGATEWAY_USER_AUTH"] = "1"
        env["ABSTRACTGATEWAY_USERS_FILE"] = str(Path(data_dir).parent / "users.json")
        env["ABSTRACTGATEWAY_SESSIONS_FILE"] = str(Path(data_dir).parent / "sessions.json")
    else:
        env.pop("ABSTRACTGATEWAY_USER_AUTH", None)
    env.update(extra_env or {})
    return env


def start(tree: Tree, data_dir: Path, *, ops: Optional[List[Dict[str, Any]]] = None, flows_dir: Optional[Path] = None,
          user_auth: bool = False, users: Iterable[str] = (), tokens: Optional[Dict[str, str]] = None,
          runner: bool = True, extra_env: Optional[Dict[str, str]] = None, timeout_s: float = 600.0) -> Start:
    """One `abstractgateway serve` from `tree` on `data_dir` (`--flows-dir flows_dir` when given);
    runs `ops` (tests/workflow_folder_driver.py)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    scratch = data_dir.parent / ".driver"
    scratch.mkdir(exist_ok=True)
    n = len(list(scratch.glob("spec-*.json")))
    spec_path, out_path = scratch / f"spec-{n}.json", scratch / f"out-{n}.json"
    spec = {"package_dir": str(tree.package_dir), "admin_token": ADMIN_TOKEN, "ops": list(ops or []),
            "users": list(users), "tokens": dict(tokens or {}), "flows_dir": str(flows_dir) if flows_dir else None}
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    env = gateway_env(tree, data_dir, user_auth=user_auth, runner=runner, extra_env=extra_env)
    proc = subprocess.run([sys.executable, str(DRIVER), str(spec_path), str(out_path)], env=env, cwd=str(data_dir.parent),
                          capture_output=True, text=True, timeout=timeout_s)
    results = json.loads(out_path.read_text(encoding="utf-8")) if proc.returncode == 0 and out_path.is_file() else {}
    return Start(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, results=results)


def automation_body(request_id: str, title: str, workflow: str = PUBLISHED_WORKFLOW) -> Dict[str, Any]:
    ref, flow = workflow.rsplit(":", 1)
    return {
        "request_id": request_id,
        "title": title,
        "target": {"bundle_ref": ref, "flow_id": flow, "input_data": {"prompt": "hello"}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
        "context": {"mode": "independent"},
    }
