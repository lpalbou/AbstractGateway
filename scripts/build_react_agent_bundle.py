#!/usr/bin/env python3
"""Build the react-agent native-loop WorkflowBundle (gateway loop-surfacing).

Manifest-only bundle: ``metadata.native_loop_factory: react`` with empty
``flows``. The gateway materializes the abstractagent ReAct loop at load time
(see ``native_loop_bundles.materialize_native_loop_specs``).

Usage: build_react_agent_bundle.py [--version 0.1.0] [--out <dir>]
Writes react-agent@<version>.flow (zip with manifest.json only).
"""
from __future__ import annotations

import argparse
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BUNDLE_ID = "react-agent"
ENTRYPOINT_ID = "react"
AGENT_INTERFACE = "abstractcode.agent.v1"


def build_manifest(version: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "bundle_format_version": "1",
        "bundle_id": BUNDLE_ID,
        "bundle_version": version,
        "created_at": now,
        "entrypoints": [
            {
                "flow_id": ENTRYPOINT_ID,
                "name": "react",
                "description": "Native ReAct agent loop (abstractagent)",
                "interfaces": [AGENT_INTERFACE],
            }
        ],
        "default_entrypoint": ENTRYPOINT_ID,
        "flows": {},
        "artifacts": {},
        "assets": {},
        "metadata": {
            "native_loop_factory": "react",
            "loop_family": "react",
            "publisher": {
                "host": "abstractgateway.scripts",
                "published_at": now,
            },
        },
    }


def build_bundle(version: str, out_dir: Path) -> Path:
    manifest = build_manifest(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{BUNDLE_ID}@{version}.flow"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="0.1.0")
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "flows" / "bundles"),
    )
    args = ap.parse_args()
    built = build_bundle(args.version, Path(args.out))
    print(f"built {built}")
