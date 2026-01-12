from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple


def _load_json_file(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json_file(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return


def resolve_embedding_config(*, base_dir: Path) -> Tuple[str, str]:
    """Resolve the gateway-wide embedding provider/model.

    Contract:
    - Stable within a single gateway instance (singleton embedding space).
    - Persisted under the gateway data dir so restarts keep the same embedding space by default.
    - Env vars can override (and will re-write the persisted value).
    """
    provider_env = os.getenv("ABSTRACTGATEWAY_EMBEDDING_PROVIDER") or os.getenv("ABSTRACTFLOW_EMBEDDING_PROVIDER")
    model_env = os.getenv("ABSTRACTGATEWAY_EMBEDDING_MODEL") or os.getenv("ABSTRACTFLOW_EMBEDDING_MODEL")

    cfg_path = Path(base_dir) / "gateway_embeddings.json"
    cfg = _load_json_file(cfg_path) if cfg_path.exists() else {}

    provider = str(provider_env or cfg.get("provider") or "lmstudio").strip().lower()
    model = str(model_env or cfg.get("model") or "text-embedding-nomic-embed-text-v1.5@q6_k").strip()

    # Persist for stability across restart (best-effort).
    _save_json_file(cfg_path, {"provider": provider, "model": model})
    return provider, model

