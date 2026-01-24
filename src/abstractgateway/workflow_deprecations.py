from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _empty_state() -> Dict[str, Any]:
    return {"version": 1, "bundles": {}}


def _normalize_state(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_state()
    bundles0 = raw.get("bundles")
    if not isinstance(bundles0, dict):
        return _empty_state()
    bundles: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for bid, flows in bundles0.items():
        b = str(bid or "").strip()
        if not b:
            continue
        if not isinstance(flows, dict):
            continue
        out_flows: Dict[str, Dict[str, Any]] = {}
        for fid, rec in flows.items():
            f = str(fid or "").strip() or "*"
            if not isinstance(rec, dict):
                continue
            deprecated_at = str(rec.get("deprecated_at") or "").strip() or ""
            reason = str(rec.get("reason") or "").strip() or ""
            deprecated_by = str(rec.get("deprecated_by") or "").strip() or ""
            out_flows[f] = {
                "deprecated_at": deprecated_at,
                **({"reason": reason} if reason else {}),
                **({"deprecated_by": deprecated_by} if deprecated_by else {}),
            }
        if out_flows:
            bundles[b] = out_flows
    return {"version": 1, "bundles": bundles}


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp_fd: Optional[int] = None
    tmp_path: Optional[str] = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(str(tmp_path), str(path))
        tmp_path = None
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except Exception:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


class WorkflowDeprecatedError(RuntimeError):
    def __init__(self, *, bundle_id: str, flow_id: str, record: Dict[str, Any]):
        self.bundle_id = str(bundle_id or "").strip()
        self.flow_id = str(flow_id or "").strip() or "*"
        self.record = dict(record or {})
        reason = str(self.record.get("reason") or "").strip()
        msg = f"Workflow '{self.bundle_id}:{self.flow_id}' is deprecated"
        if reason:
            msg = f"{msg}: {reason}"
        super().__init__(msg)


class WorkflowDeprecationStore:
    """File-backed store for workflow deprecation status (gateway-owned).

    Format (v1):
    {
      "version": 1,
      "bundles": {
        "bundle_id": {
          "*": { "deprecated_at": "...", "reason": "..." },
          "flow_id": { "deprecated_at": "...", "reason": "..." }
        }
      }
    }
    """

    def __init__(self, *, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _load_unlocked(self) -> Tuple[Dict[str, Any], Optional[str]]:
        if not self._path.exists():
            return _empty_state(), None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return _normalize_state(raw), None
        except Exception as e:
            # Best-effort recovery: keep a copy for inspection and start fresh.
            try:
                ts = _utc_now_iso().replace(":", "").replace("-", "")
                backup = self._path.with_suffix(self._path.suffix + f".corrupt.{ts}")
                os.replace(str(self._path), str(backup))
            except Exception:
                pass
            return _empty_state(), str(e)

    def snapshot(self) -> Tuple[Dict[str, Any], Optional[str]]:
        with self._lock:
            return self._load_unlocked()

    def get_record(self, *, bundle_id: str, flow_id: str) -> Optional[Dict[str, Any]]:
        bid = str(bundle_id or "").strip()
        fid = str(flow_id or "").strip() or "*"
        if not bid:
            return None
        with self._lock:
            state, _err = self._load_unlocked()
            bundles = state.get("bundles")
            if not isinstance(bundles, dict):
                return None
            flows = bundles.get(bid)
            if not isinstance(flows, dict):
                return None
            specific = flows.get(fid)
            if isinstance(specific, dict) and specific.get("deprecated_at"):
                return dict(specific)
            wildcard = flows.get("*")
            if isinstance(wildcard, dict) and wildcard.get("deprecated_at"):
                return dict(wildcard)
            return None

    def is_deprecated(self, *, bundle_id: str, flow_id: str) -> bool:
        return self.get_record(bundle_id=bundle_id, flow_id=flow_id) is not None

    def set_deprecated(
        self,
        *,
        bundle_id: str,
        flow_id: Optional[str] = None,
        reason: Optional[str] = None,
        deprecated_by: Optional[str] = None,
        deprecated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = str(bundle_id or "").strip()
        if not bid:
            raise ValueError("bundle_id is required")
        fid = str(flow_id or "").strip() or "*"
        rec: Dict[str, Any] = {"deprecated_at": str(deprecated_at or "").strip() or _utc_now_iso()}
        r = str(reason or "").strip()
        if r:
            rec["reason"] = r
        by = str(deprecated_by or "").strip()
        if by:
            rec["deprecated_by"] = by
        with self._lock:
            state, err = self._load_unlocked()
            if err:
                logger.warning("Deprecation store reset due to parse error", extra={"path": str(self._path), "error": err})
            bundles = state.setdefault("bundles", {})
            if not isinstance(bundles, dict):
                bundles = {}
                state["bundles"] = bundles
            flows = bundles.get(bid)
            if not isinstance(flows, dict):
                flows = {}
                bundles[bid] = flows
            flows[fid] = rec
            _atomic_write_json(self._path, _normalize_state(state))
        return {"bundle_id": bid, "flow_id": fid, **rec}

    def clear_deprecated(self, *, bundle_id: str, flow_id: Optional[str] = None) -> bool:
        bid = str(bundle_id or "").strip()
        if not bid:
            raise ValueError("bundle_id is required")
        fid = str(flow_id or "").strip() or "*"
        with self._lock:
            state, err = self._load_unlocked()
            if err:
                logger.warning("Deprecation store reset due to parse error", extra={"path": str(self._path), "error": err})
            bundles = state.get("bundles")
            if not isinstance(bundles, dict):
                return False
            flows = bundles.get(bid)
            if not isinstance(flows, dict):
                return False
            if fid not in flows:
                return False
            del flows[fid]
            if not flows:
                try:
                    del bundles[bid]
                except Exception:
                    pass
            _atomic_write_json(self._path, _normalize_state(state))
            return True

