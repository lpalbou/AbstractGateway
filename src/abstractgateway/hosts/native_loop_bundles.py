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


def _allowed_tools_from_metadata(metadata: Dict[str, Any]) -> Optional[list[str]]:
    raw = metadata.get("allowed_tools")
    if not isinstance(raw, (list, tuple)):
        return None
    allowed: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name:
            allowed.append(name)
    return allowed or None


def _legacy_default_react_memact_tools() -> list[Any]:
    from abstractagent.tools import ALL_TOOLS

    return list(ALL_TOOLS)


def _legacy_default_codeact_tools() -> list[Any]:
    from abstractagent.logic.builtins import (
        ASK_USER_TOOL,
        COMPACT_MEMORY_TOOL,
        DELEGATE_AGENT_TOOL,
        INSPECT_VARS_TOOL,
        OPEN_ATTACHMENT_TOOL,
        RECALL_MEMORY_TOOL,
        REMEMBER_NOTE_TOOL,
        REMEMBER_TOOL,
    )
    from abstractagent.tools.code_execution import execute_python

    return [
        ASK_USER_TOOL,
        OPEN_ATTACHMENT_TOOL,
        RECALL_MEMORY_TOOL,
        INSPECT_VARS_TOOL,
        REMEMBER_TOOL,
        REMEMBER_NOTE_TOOL,
        COMPACT_MEMORY_TOOL,
        DELEGATE_AGENT_TOOL,
        execute_python,
    ]


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
    try:
        from abstractagent.adapters.native_loop_registry import materialize_native_loop_spec
    except ImportError as exc:
        if getattr(exc, "name", None) != "abstractagent.adapters.native_loop_registry":
            raise
        return _materialize_factory_spec_legacy(
            factory=factory,
            bundle_ref=bundle_ref,
            entrypoint=entrypoint,
            workflow_id=workflow_id,
            metadata=metadata,
        )

    spec = materialize_native_loop_spec(
        factory,
        bundle_ref=bundle_ref,
        entrypoint=entrypoint,
        metadata=metadata,
    )
    if str(spec.workflow_id) != str(workflow_id):
        spec = replace(spec, workflow_id=str(workflow_id))
    return spec


def _materialize_factory_spec_legacy(
    *,
    factory: str,
    bundle_ref: str,
    entrypoint: str,
    workflow_id: str,
    metadata: Dict[str, Any],
) -> WorkflowSpec:
    """Compat path for abstractagent builds older than native_loop_registry."""
    allowed_tools = _allowed_tools_from_metadata(metadata)
    logger.info(
        "Falling back to legacy native-loop materialization for %s because "
        "abstractagent.adapters.native_loop_registry is unavailable",
        bundle_ref,
    )

    if factory == "react":
        from abstractagent.adapters.react_runtime import create_react_workflow
        from abstractagent.logic.react import ReActLogic

        kwargs: Dict[str, Any] = {
            "logic": ReActLogic(tools=_legacy_default_react_memact_tools()),
            "workflow_id": str(workflow_id),
        }
        if allowed_tools is not None:
            kwargs["allowed_tools"] = allowed_tools
        try:
            return create_react_workflow(**kwargs)
        except TypeError:
            kwargs.pop("allowed_tools", None)
            return create_react_workflow(**kwargs)

    if factory == "codeact":
        from abstractagent.adapters.codeact_runtime import create_codeact_workflow
        from abstractagent.logic.codeact import CodeActLogic

        spec = create_codeact_workflow(
            logic=CodeActLogic(tools=_legacy_default_codeact_tools())
        )
        if str(spec.workflow_id) != str(workflow_id):
            spec = replace(spec, workflow_id=str(workflow_id))
        return spec

    from abstractagent.adapters.memact_runtime import create_memact_workflow
    from abstractagent.logic.memact import MemActLogic

    kwargs = {
        "logic": MemActLogic(tools=_legacy_default_react_memact_tools()),
        "workflow_id": str(workflow_id),
    }
    if allowed_tools is not None:
        kwargs["allowed_tools"] = allowed_tools
    try:
        return create_memact_workflow(**kwargs)
    except TypeError:
        kwargs.pop("allowed_tools", None)
        return create_memact_workflow(**kwargs)
