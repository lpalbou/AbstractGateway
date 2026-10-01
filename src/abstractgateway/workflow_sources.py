"""Where a workflow bundle came from (DESIGN-v2 §4.1, §6): `shipped` | `published` | `imported`.

The gateway writes imports (POST /bundles/upload) and AbstractFlow publishes
(POST /visualflows/{id}/publish) into the same registry folder as the bundles it ships (on the
default runtime that folder IS the packaged flows folder), so the folder alone cannot tell them
apart. The rule, in order:

1. shipped: the file name is one the installed package ships — the `.flow` files under
   `abstractgateway/flows/bundles/` in the distribution's file list (wheel RECORD), or, in a
   source checkout, the wheel force-include list of the repository's own pyproject.toml (the
   list the wheel is built from);
2. published: the manifest carries the publish route's stamp
   (`metadata.publisher.host == "abstractgateway"` and `metadata.lifecycle.source ==
   "abstractflow.editor"`, routes/gateway.py publish);
3. imported: anything else (an uploaded .flow, or a file someone copied into the folder).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

PACKAGED_PREFIX = "abstractgateway/flows/bundles/"


@functools.lru_cache(maxsize=1)
def shipped_bundle_names() -> FrozenSet[str]:
    names = set()
    try:
        from importlib.metadata import PackageNotFoundError, files

        try:
            for f in files("abstractgateway") or []:
                text = str(f).replace("\\", "/")
                if text.startswith(PACKAGED_PREFIX) and text.endswith(".flow"):
                    names.add(text.rsplit("/", 1)[-1])
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001 - a broken RECORD falls through to the checkout list
        pass
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            doc = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            include = (((doc.get("tool") or {}).get("hatch") or {}).get("build") or {}).get("targets", {}).get("wheel", {}).get("force-include") or {}
            for src in include:
                text = str(src).replace("\\", "/")
                if text.startswith("flows/bundles/") and text.endswith(".flow"):
                    names.add(text.rsplit("/", 1)[-1])
        except Exception:  # noqa: BLE001
            pass
    return frozenset(names)


def bundle_source(path: Optional[str], metadata: Optional[Dict[str, Any]]) -> str:
    name = Path(str(path)).name if path else ""
    if name and name in shipped_bundle_names():
        return "shipped"
    meta = metadata if isinstance(metadata, dict) else {}
    publisher = meta.get("publisher") if isinstance(meta.get("publisher"), dict) else {}
    lifecycle = meta.get("lifecycle") if isinstance(meta.get("lifecycle"), dict) else {}
    if str(publisher.get("host") or "") == "abstractgateway" and str(lifecycle.get("source") or "") == "abstractflow.editor":
        return "published"
    return "imported"
