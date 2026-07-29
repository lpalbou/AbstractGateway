#!/usr/bin/env python3
"""Build a manifest-only native-loop WorkflowBundle for the gateway catalog.

Usage:
  build_native_loop_bundle.py --factory react --bundle-id react-agent
  build_native_loop_bundle.py --factory codeact --bundle-id codeact-agent
  build_native_loop_bundle.py --factory memact --bundle-id memact-agent
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

AGENT_INTERFACE = "abstractcode.agent.v1"
FACTORIES = frozenset({"react", "codeact", "memact"})


def build_manifest(*, bundle_id: str, factory: str, version: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    entrypoint = factory
    return {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": version,
        "created_at": now,
        "entrypoints": [
            {
                "flow_id": entrypoint,
                "name": entrypoint,
                "description": f"Native {factory} agent loop (abstractagent)",
                "interfaces": [AGENT_INTERFACE],
            }
        ],
        "default_entrypoint": entrypoint,
        "flows": {},
        "artifacts": {},
        "assets": {},
        "metadata": {
            "native_loop_factory": factory,
            "loop_family": factory,
            "publisher": {
                "host": "abstractgateway.scripts",
                "published_at": now,
            },
        },
    }


def build_bundle(*, bundle_id: str, factory: str, version: str, out_dir: Path) -> Path:
    if factory not in FACTORIES:
        raise SystemExit(f"unsupported factory {factory!r} (expected react|codeact|memact)")
    manifest = build_manifest(bundle_id=bundle_id, factory=factory, version=version)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{bundle_id}@{version}.flow"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--factory", required=True, choices=sorted(FACTORIES))
    ap.add_argument("--bundle-id", required=True)
    ap.add_argument("--version", default="0.1.0")
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "flows" / "bundles"),
    )
    args = ap.parse_args()
    built = build_bundle(
        bundle_id=str(args.bundle_id).strip(),
        factory=str(args.factory).strip().lower(),
        version=str(args.version).strip(),
        out_dir=Path(args.out),
    )
    print(f"built {built}")


if __name__ == "__main__":
    main()
