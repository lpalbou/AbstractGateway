#!/usr/bin/env python3
"""Reproducibly package canonical Assistant + shipped basic-agent subflows.

Run from the framework checkout. No catalog export, network, or installed app
state is used. Existing published bundle versions are never overwritten with
changed bytes; choose a new version when changing canonical source.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

GATEWAY = Path(__file__).resolve().parents[1]
ASSISTANT = GATEWAY.parent / 'abstractassistant'
BUNDLE_ID = 'abstractassistant-orchestrator'


def canonical_workflow():
    """Load the exact maintainer checkout only when rebuilding, never at import."""
    path = ASSISTANT / 'abstractassistant/assistant_workflow.py'
    if not path.is_file():
        raise FileNotFoundError(
            f'Rebuilding requires the canonical sibling Assistant checkout: {path}. '
            'Running the shipped bundle does not require AbstractAssistant.')
    spec = importlib.util.spec_from_file_location('_assistant_bundle_canonical', path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through the defining module registry.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.MANAGED_ASSISTANT_WORKFLOW_BUNDLE_ID == BUNDLE_ID
    return module


ENTRY = 'c53b1579'
LEGACY_ENTRY = 'd5d4e5a1'


def bundle_bytes(version: str = '0.0.15') -> bytes:
    import io
    canonical = canonical_workflow()
    basic_path = GATEWAY / 'flows/bundles/basic-agent.flow'
    with zipfile.ZipFile(basic_path) as source:
        basic = json.loads(source.read('manifest.json'))
        flows = {fid: json.loads(source.read(path)) for fid, path in basic['flows'].items()}
    agent_id = basic['default_entrypoint']
    agent = canonical.agent_subflow_from_flow(flow_id=agent_id,
        bundle_ref=f"{basic['bundle_id']}@{basic['bundle_version']}", flow=flows[agent_id])
    root = canonical.managed_assistant_visualflow(agent)
    root['id'] = ENTRY
    flows[ENTRY] = root
    # Versionless account preferences retain their exact entrypoint ID across upgrades.
    # Both IDs are public manifest entrypoints; resolution must never silently fall back.
    flows[LEGACY_ENTRY] = {**root, 'id': LEGACY_ENTRY, 'name': root['name'] + ' (legacy entrypoint)'}
    manifest = {
        'bundle_format_version': '1', 'bundle_id': BUNDLE_ID,
        'bundle_version': version, 'created_at': '2026-10-18T00:00:00+00:00',
        'entrypoints': [{'flow_id': fid, 'name': flows[fid]['name'], 'description': root['description'], 'interfaces': root['interfaces']} for fid in (ENTRY, LEGACY_ENTRY)],
        'default_entrypoint': ENTRY, 'flows': {fid: f'flows/{fid}.json' for fid in sorted(flows)},
        'artifacts': {}, 'assets': {},
        'metadata': {'managed_workflow_revision': canonical.MANAGED_ASSISTANT_WORKFLOW_REVISION,
            'source': 'abstractassistant/assistant_workflow.py',
            'basic_agent_sha256': hashlib.sha256(basic_path.read_bytes()).hexdigest()},
    }
    entries = {'manifest.json': manifest, **{f'flows/{fid}.json': flow for fid, flow in flows.items()}}
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 18, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n')
    return result.getvalue()


def build_bundle(version: str, out_dir: Path) -> Path:
    data = bundle_bytes(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f'{BUNDLE_ID}@{version}.flow'
    if path.exists() and path.read_bytes() != data:
        raise ValueError(f'Refusing to change existing version {path}; choose a new version')
    path.write_bytes(data)
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='0.0.15')
    parser.add_argument('--out', type=Path, default=GATEWAY / 'flows/bundles')
    args = parser.parse_args()
    print(build_bundle(args.version, args.out))
