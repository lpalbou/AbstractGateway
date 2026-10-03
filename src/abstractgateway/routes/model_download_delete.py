"""`POST /api/gateway/models/delete-download`: the Models page's "Delete download".

Admin only (policy row in `security/authorization.py` AND
`_require_admin_principal`). Body `{provider, artifact, dry_run}`; answers
`model_download_delete_v1` (see `model_download_delete.py`):

  200  {status: "planned", freed_bytes, ...}    dry_run: what a delete would free
  200  {status: "deleted", freed_bytes, presence: "absent", paths, command}
  409  {status: "refused", reason: resident|locked|downloading|managed_elsewhere|
        engine_not_running|unknown_location|remote_engine, message, fix}
  404  {status: "not_found", reason: "not_downloaded"}
  502  {status: "failed", reason: "engine_failed", message, fix}

The removal runs synchronously (an Ollama delete or a cache-folder removal
takes well under a second); `POST /models/delete` keeps its job lane for the
CLI and the terminal console.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/gateway", tags=["models"])


class DeleteDownloadRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit", "dry_run": True}]},
    )

    provider: str = Field(..., max_length=120, description="The artifact's provider as the model catalog names it (ollama | mlx | huggingface | lmstudio ...).")
    artifact: str = Field(..., max_length=400, description="The artifact id as the model catalog names it (`org/repo`, `org/repo:QUANT`, `model:tag`).")
    dry_run: bool = Field(default=False, description="Measure what would be freed and run every refusal, deleting nothing.")


def _resident_rows() -> List[Dict[str, Any]]:
    """What this gateway holds in memory (`model_residency_row_v1`)."""

    from .gateway import _gateway_model_residency_client_call, _model_residency_rows_v1

    payload = _gateway_model_residency_client_call(method_name="list_model_residency", operation="list_loaded", payload={})
    return _model_residency_rows_v1(payload if isinstance(payload, dict) else {})


@router.post(
    "/models/delete-download",
    summary="Delete a downloaded model's files (admin)",
    description=(
        "Removes one downloaded artifact with its engine's own mechanism (Ollama's delete, the Hugging Face / MLX cache, "
        "one GGUF quant). Refused while the model is loaded or locked (\"Unload it first\"), while its download runs, and "
        "for LM Studio (managed in LM Studio). `dry_run` answers the bytes it would free. Audited."
    ),
)
async def delete_download_route(req: DeleteDownloadRequest, request: Request) -> Any:
    from ..model_download_delete import DownloadDeleteRefused, delete_download
    from .gateway import _require_admin_principal

    principal = _require_admin_principal(request)
    actor = str(getattr(principal, "user_id", "") or "")
    try:
        out = await asyncio.to_thread(
            delete_download, req.provider, req.artifact, dry_run=req.dry_run, actor=actor, resident_rows=_resident_rows
        )
    except DownloadDeleteRefused as exc:
        _note(request, req, str(exc.body.get("status")))
        return JSONResponse(status_code=exc.status_code, content=jsonable_encoder(exc.body))
    _note(request, req, str(out.get("status")))
    return out


def _note(request: Request, req: DeleteDownloadRequest, outcome: str) -> None:
    """Name the model and outcome on this request's audit line."""

    try:
        detail = getattr(request.state, "audit_detail", None)
        detail = dict(detail) if isinstance(detail, dict) else {}
        detail["model_download_delete"] = {"provider": req.provider, "artifact": req.artifact, "dry_run": req.dry_run, "outcome": outcome}
        request.state.audit_detail = detail
    except Exception:  # noqa: BLE001 - auditing never breaks the request
        pass
