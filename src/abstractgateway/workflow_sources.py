"""Where a workflow bundle came from (DESIGN-v2 §4.1, §6): `shipped` | `published` | `imported`.

The gateway writes imports (POST /bundles/upload) and AbstractFlow publishes
(POST /visualflows/{id}/publish) into the same registry folder as the bundles it ships (on the
default runtime that folder IS the gateway's own flows folder, config.py `_default_flows_dir`),
so the folder alone cannot tell them apart. The rule, in order:

1. shipped: a file this gateway ships in its flows folder —
   - an installed package: the `.flow` files under `abstractgateway/flows/bundles/` in the
     distribution's file list (wheel RECORD);
   - a repository checkout (a deploy that runs from the source tree): every `.flow` file the
     repository itself keeps in `flows/bundles/`. That folder is git-ignored except for the
     files the repository ships, each named by an explicit `!flows/bundles/<file>.flow` line in
     the repository's .gitignore; the wheel force-include list of pyproject.toml (a subset:
     map-reduce, structured-extract, adversarial-review, the meta-* agents and older versions
     are kept in the checkout but not packed into the wheel) counts too.
   An import or a publish written into that folder is git-ignored, so it is never on either list;
2. published: the manifest carries the publish route's stamp
   (`metadata.publisher.host == "abstractgateway"` and `metadata.lifecycle.source ==
   "abstractflow.editor"`, routes/gateway.py publish);
3. imported: anything else (an uploaded .flow, or a file someone copied into the folder).
"""

from __future__ import annotations

import sys
import functools
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Set

PACKAGED_PREFIX = "abstractgateway/flows/bundles/"
CHECKOUT_PREFIX = "flows/bundles/"


def _flow_name(text: str, prefix: str) -> Optional[str]:
    text = str(text).replace("\\", "/").strip()
    if text.startswith(prefix) and text.endswith(".flow") and "/" not in text[len(prefix):]:
        return text[len(prefix):]
    return None


def checkout_shipped_names(repo_root: Path) -> FrozenSet[str]:
    """The `.flow` files a repository checkout ships in `flows/bundles/`: the explicit
    `!flows/bundles/<file>.flow` negations of its .gitignore plus its wheel force-include list.
    Empty when `repo_root` is not a checkout (no pyproject.toml)."""
    names: Set[str] = set()
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return frozenset()
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # Python 3.10: the  backport (a dependency below 3.11, pyproject.toml)
        import tomli as tomllib

    doc = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    include = (((doc.get("tool") or {}).get("hatch") or {}).get("build") or {}).get("targets", {}).get("wheel", {}).get("force-include") or {}
    for src in include:
        name = _flow_name(src, CHECKOUT_PREFIX)
        if name:
            names.add(name)
    gitignore = repo_root / ".gitignore"
    if gitignore.is_file():
        for line in gitignore.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("!"):
                name = _flow_name(line[1:].lstrip("/"), CHECKOUT_PREFIX)
                if name:
                    names.add(name)
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def shipped_bundle_names() -> FrozenSet[str]:
    names: Set[str] = set()
    try:
        from importlib.metadata import PackageNotFoundError, files

        try:
            for f in files("abstractgateway") or []:
                name = _flow_name(str(f), PACKAGED_PREFIX)
                if name:
                    names.add(name)
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001 - a broken RECORD falls through to the checkout list
        pass
    names |= checkout_shipped_names(Path(__file__).resolve().parent.parent.parent)
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
