"""Boot ensure-publish of shipped catalog bundles (card 013).

A fresh gateway install ships the console assistant's transport bundle
(docs-qa) in the wheel, but the tenant catalog starts EMPTY — the drawer had
a UI affordance with an honest "Bundle 'docs-qa' not found" behind it until
an admin ran the documented curl. This module closes the out-of-box gap with
the narrowest correct shape (card 013 decision, shape (a)):

- WHICH BUNDLES: an explicit list — docs-qa only. The other force-included
  bundles (basic-agent, abstractassistant-orchestrator, dp-research) load
  through the PRIVATE runtime registry at boot and never needed the catalog;
  auto-publishing them would mint parallel ACL'd copies of bundles that
  already work.
- PUBLISH IF ABSENT, by exact version: a restart never churns records,
  never touches updated_at, and never overwrites an admin's publisher
  attribution. The one repair case — record present but the catalog bundle
  FILE missing from disk — reinstalls the file preserving the existing
  record's publisher.
- ADMIN AUTHORITY SURVIVES: make_default=False (the store only assigns a
  default when none exists, so a fresh catalog gets one and an admin-moved
  pointer is never moved back); a tombstoned/blocked version is skipped,
  never resurrected; a sha conflict (someone rebuilt the bundle at the same
  version) warns LOUDLY naming the repair (bump the version) and never
  blocks boot.
- PUBLISHER PRINCIPAL: "system:gateway-boot" — catalog records carry a
  publisher string; a boot-time act must not impersonate a user.
- KILL SWITCH: ABSTRACTGATEWAY_AUTO_PUBLISH_SHIPPED=0 restores the
  status-quo posture (documented curl only).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Explicit allowlist of shipped bundles that belong in the tenant catalog
# (card 013: "which shipped bundles ride" — the answer is a NAMED list, not
# a directory glob; adding one is a deliberate act).
SHIPPED_CATALOG_BUNDLE_IDS = ("docs-qa",)

BOOT_PUBLISHER = "system:gateway-boot"


def auto_publish_shipped_enabled() -> bool:
    raw = str(os.getenv("ABSTRACTGATEWAY_AUTO_PUBLISH_SHIPPED") or "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


def _bundle_file_uses_llm(path: Path) -> bool:
    """Does one .flow bundle carry any llm_call/agent node? Reuses the
    host's own node scanner so the gate and the host agree on what "uses
    LLM" means. Unreadable bundles count as NOT using LLM (conservative:
    an unproven requirement must not open the gate)."""
    import json as _json
    import zipfile

    from .hosts.bundle_host import _flow_uses_llm

    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not (name.startswith("flows/") and name.endswith(".json")):
                    continue
                try:
                    raw = _json.loads(zf.read(name).decode("utf-8"))
                except Exception:
                    continue
                if isinstance(raw, dict) and _flow_uses_llm(raw):
                    return True
    except Exception:
        return False
    return False


def _publish_is_boot_neutral(flows_dir: Optional[Path]) -> bool:
    """The BOOT-NEUTRALITY invariant: adding docs-qa to the catalog must
    never create an LLM-construction requirement the deployment did not
    already have. The bundle host builds ONE LLM runtime at load whenever
    ANY loaded flow carries LLM/agent nodes — on the shipped default
    registry that requirement already exists (basic-agent carries an agent
    node AND an embedded provider/model pair, so resolution always
    succeeds), which makes docs-qa ride for free. A deployment whose
    private registry carries NO LLM-bearing flow may run with no provider
    at all and boots fine today; publishing an llm_call-bearing bundle
    into its catalog would turn its next boot into a refusal (found by the
    full suite: 26 custom-flows-dir tests died on the first draft of this
    hook). The gate reads flow CONTENT, not filenames — a minimal stand-in
    named basic-agent.flow proves nothing about LLM requirements.
    flows_dir=None skips the gate (direct-call posture: the caller owns
    the judgment)."""
    if flows_dir is None:
        return True
    try:
        flow_files = sorted(Path(flows_dir).glob("*.flow"))
    except OSError:
        return False
    return any(_bundle_file_uses_llm(p) for p in flow_files)


def _shipped_bundle_dirs() -> List[Path]:
    """Where shipped .flow artifacts live: the wheel-packaged directory first
    (abstractgateway/flows/bundles — pyproject force-include), then the repo
    checkout's flows/bundles (dev installs). Deliberately NOT cfg.flows_dir:
    an operator override redirects the private registry, but the shipped
    artifact we publish is the one this package carries."""
    package_root = Path(__file__).resolve().parent
    return [
        package_root / "flows" / "bundles",
        package_root.parent.parent / "flows" / "bundles",
    ]


def _find_shipped_bundle_files(bundle_id: str, search_dirs: Optional[List[Path]] = None) -> List[Path]:
    """All shipped versions of one bundle id, e.g. docs-qa@0.1.0.flow.
    First directory that carries any match wins (wheel beats repo — they are
    the same bytes in a consistent build; a divergent repo copy must not
    shadow what the installed package actually ships)."""
    for directory in search_dirs if search_dirs is not None else _shipped_bundle_dirs():
        try:
            if not directory.is_dir():
                continue
            matches = sorted(p for p in directory.glob(f"{bundle_id}@*.flow") if p.is_file())
        except OSError:
            continue
        if matches:
            return matches
    return []


def _version_from_filename(path: Path, bundle_id: str) -> str:
    stem = path.name[: -len(".flow")] if path.name.endswith(".flow") else path.name
    prefix = f"{bundle_id}@"
    return stem[len(prefix):] if stem.startswith(prefix) else ""


def ensure_shipped_catalog_bundles(
    *,
    root_data_dir: Path,
    tenant_id: str = "default",
    flows_dir: Optional[Path] = None,
    search_dirs: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """Idempotently ensure the shipped catalog bundles are published into
    this tenant's catalog. Best-effort BY CONTRACT: a broken catalog or a
    conflicting record warns loudly and never blocks a boot (same posture as
    the boot data-homes registration).

    `flows_dir` is the deployment's private bundle registry — passed by the
    boot path so the boot-neutrality gate can tell "shipped default
    registry" (basic-agent present, LLM construction already required) from
    "custom minimal deployment" (publishing an LLM-bearing bundle would
    CREATE a boot requirement).

    Returns a summary dict: {enabled, published: [...], skipped: [...],
    warnings: [...]} — callers may log or ignore it."""
    summary: Dict[str, Any] = {"enabled": True, "published": [], "skipped": [], "warnings": []}
    if not auto_publish_shipped_enabled():
        summary["enabled"] = False
        return summary

    if not _publish_is_boot_neutral(flows_dir):
        msg = (
            "shipped-catalog publish skipped: this deployment's flows dir carries no "
            "basic-agent (custom-bundle posture) — publishing the LLM-bearing docs-qa "
            "would add a boot requirement the deployment never had; publish it manually "
            "if wanted (docs/api.md §2d)"
        )
        logger.info("%s", msg)
        summary["skipped"].append({"reason": "not_boot_neutral"})
        return summary

    try:
        from .workflow_catalog import (
            CATALOG_SCOPE_TENANT,
            WorkflowCatalogError,
            WorkflowCatalogStore,
        )

        store = WorkflowCatalogStore(root_data_dir=Path(root_data_dir))
    except Exception as e:
        msg = f"shipped-catalog publish unavailable (catalog store failed to open): {e}"
        logger.warning("%s", msg)
        summary["warnings"].append(msg)
        return summary

    for bundle_id in SHIPPED_CATALOG_BUNDLE_IDS:
        files = _find_shipped_bundle_files(bundle_id, search_dirs)
        if not files:
            # Legitimate for source layouts that did not build the bundle —
            # note it once so a fresh-install report can explain the drawer.
            msg = f"shipped bundle '{bundle_id}' not found in package/repo flows/bundles — catalog publish skipped"
            logger.info("%s", msg)
            summary["skipped"].append({"bundle_id": bundle_id, "reason": "artifact_missing"})
            continue

        for path in files:
            version = _version_from_filename(path, bundle_id)
            if not version:
                continue
            ref = f"{bundle_id}@{version}"
            try:
                existing = store.get_record(
                    scope=CATALOG_SCOPE_TENANT, tenant_id=tenant_id, bundle_id=bundle_id, bundle_version=version
                )
                content = path.read_bytes()
                if existing is not None:
                    bundle_file = Path(str(existing.get("path") or ""))
                    if str(existing.get("path") or "") and not bundle_file.is_file():
                        # Repair: metadata survived a wiped bundles dir. The
                        # install path re-writes the file; publisher=None
                        # preserves the existing record's attribution.
                        store.install_bundle_bytes(
                            content,
                            scope=CATALOG_SCOPE_TENANT,
                            tenant_id=tenant_id,
                            make_default=False,
                            publisher=None,
                        )
                        summary["published"].append({"bundle_ref": ref, "action": "file_restored"})
                    else:
                        # Present in ANY status: a tombstoned/blocked version
                        # is an admin decision — never resurrected here.
                        summary["skipped"].append({"bundle_id": bundle_id, "version": version, "reason": "already_in_catalog"})
                    continue
                record = store.install_bundle_bytes(
                    content,
                    scope=CATALOG_SCOPE_TENANT,
                    tenant_id=tenant_id,
                    make_default=False,  # the store assigns a default only when none exists
                    publisher=BOOT_PUBLISHER,
                )
                summary["published"].append(
                    {
                        "bundle_ref": ref,
                        "action": "published",
                        "is_default": bool(record.get("is_default")),
                    }
                )
                logger.info(
                    "shipped catalog bundle published: %s (tenant %s, publisher %s, default=%s)",
                    ref,
                    tenant_id,
                    BOOT_PUBLISHER,
                    bool(record.get("is_default")),
                )
            except WorkflowCatalogError as e:
                # Immutability conflict: someone rebuilt the artifact at the
                # same version. The catalog record is authoritative — warn
                # with the repair, never block the boot.
                msg = (
                    f"shipped catalog bundle {ref} conflicts with the existing catalog record: {e} "
                    "— bump the shipped bundle version (catalog versions are immutable by sha)"
                )
                logger.warning("%s", msg)
                summary["warnings"].append(msg)
            except Exception as e:
                msg = f"shipped catalog publish failed for {ref}: {e}"
                logger.warning("%s", msg)
                summary["warnings"].append(msg)
    return summary
