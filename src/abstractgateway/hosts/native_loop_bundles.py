"""Materialize abstractagent native loops from WorkflowBundle manifests.

Bundles declare ``metadata.native_loop_factory`` (or ``loop_family`` when
``manifest.flows`` is empty) instead of shipping VisualFlow JSON. The gateway
registers the minted :class:`WorkflowSpec` like any compiled flow.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable, Dict, Optional, Tuple

from abstractruntime import WorkflowSpec
from abstractruntime.workflow_bundle import WorkflowBundleManifest

logger = logging.getLogger(__name__)

NATIVE_LOOP_FACTORIES = frozenset({"react", "codeact", "memact"})


def declares_native_loop_bundle(manifest: WorkflowBundleManifest) -> bool:
    """True when the manifest opts into native-loop loading (even if invalid)."""
    meta = manifest.metadata if isinstance(manifest.metadata, dict) else {}
    if str(meta.get("native_loop_factory") or "").strip():
        return True
    if not manifest.flows:
        alias = str(meta.get("loop_family") or "").strip().lower()
        if alias:
            return True
    return False


def native_loop_factory(manifest: WorkflowBundleManifest) -> Optional[str]:
    """Return the factory id when this manifest is a native-loop bundle."""
    meta = manifest.metadata if isinstance(manifest.metadata, dict) else {}
    declared = str(meta.get("native_loop_factory") or "").strip().lower()
    if declared:
        return declared if declared in NATIVE_LOOP_FACTORIES else None
    if not manifest.flows:
        alias = str(meta.get("loop_family") or "").strip().lower()
        if alias in NATIVE_LOOP_FACTORIES:
            return alias
    return None


def entrypoint_input_schema_for_native_loop(*, factory: str) -> Dict[str, Any]:
    """Versioned run input schema for manifest-only native-loop entrypoints."""
    factory_key = str(factory or "").strip().lower()
    if factory_key not in NATIVE_LOOP_FACTORIES:
        raise ValueError(f"unsupported native loop factory {factory!r}")

    prompt_schema: Dict[str, Any] = {"type": "string", "title": "Prompt"}
    provider_schema: Dict[str, Any] = {"type": "string", "title": "Provider"}
    model_schema: Dict[str, Any] = {"type": "string", "title": "Model"}

    inputs: list[Dict[str, Any]] = [
        {
            "id": "prompt",
            "label": "Prompt",
            "type": "string",
            "required": True,
            "schema": dict(prompt_schema),
        },
        {
            "id": "provider",
            "label": "Provider",
            "type": "string",
            "required": False,
            "schema": dict(provider_schema),
        },
        {
            "id": "model",
            "label": "Model",
            "type": "string",
            "required": False,
            "schema": dict(model_schema),
        },
    ]

    return {
        "version": 1,
        "inputs": inputs,
        "defaults": {},
        "input_data_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "prompt": dict(prompt_schema),
                "provider": dict(provider_schema),
                "model": dict(model_schema),
            },
            "required": ["prompt"],
        },
    }


def manifest_lists_entrypoint(manifest: WorkflowBundleManifest, flow_id: str) -> bool:
    """True when ``flow_id`` names a declared bundle entrypoint."""
    needle = str(flow_id or "").strip()
    if not needle:
        return False
    for ep in manifest.entrypoints or []:
        if str(getattr(ep, "flow_id", "") or "").strip() == needle:
            return True
    return False


def materialize_native_loop_specs(
    *,
    manifest: WorkflowBundleManifest,
    bundle_ref: str,
    namespace: Callable[[str, str], str],
) -> Tuple[Dict[str, WorkflowSpec], Optional[str]]:
    """Mint workflow specs for every entrypoint in a native-loop bundle."""
    factory = native_loop_factory(manifest)
    if factory is None:
        meta = manifest.metadata if isinstance(manifest.metadata, dict) else {}
        if str(meta.get("native_loop_factory") or "").strip():
            return {}, "unsupported native_loop_factory (expected react|codeact|memact)"
        return {}, "native_loop_factory missing"

    if manifest.flows:
        return {}, "native_loop_factory bundles must not declare manifest.flows"

    meta = dict(manifest.metadata or {})
    staged: Dict[str, WorkflowSpec] = {}

    for ep in manifest.entrypoints:
        fid = str(ep.flow_id or "").strip()
        if not fid:
            continue
        ns_id = namespace(bundle_ref, fid)
        try:
            spec = _materialize_factory_spec(
                factory=factory,
                bundle_ref=bundle_ref,
                entrypoint=fid,
                workflow_id=ns_id,
                metadata=meta,
            )
        except ImportError as exc:
            return {}, f"abstractagent not importable: {exc}"
        except NotImplementedError as exc:
            return {}, str(exc)
        except Exception as exc:
            return {}, f"native loop '{factory}' materialize failed for entrypoint '{fid}': {exc}"
        staged[str(spec.workflow_id)] = spec

    if not staged:
        return {}, "native loop bundle produced zero workflow specs"
    return staged, None


def _materialize_factory_spec(
    *,
    factory: str,
    bundle_ref: str,
    entrypoint: str,
    workflow_id: str,
    metadata: Dict[str, Any],
) -> WorkflowSpec:
    from abstractagent.adapters.native_loop_registry import materialize_native_loop_spec

    spec = materialize_native_loop_spec(
        factory,
        bundle_ref=bundle_ref,
        entrypoint=entrypoint,
        metadata=metadata,
    )
    if str(spec.workflow_id) != str(workflow_id):
        spec = replace(spec, workflow_id=str(workflow_id))
    return spec
