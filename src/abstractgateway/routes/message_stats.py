"""Optional metrics for the current principal's runs; no cross-plane lookup."""
import asyncio
from fastapi import APIRouter, HTTPException
from ..service import get_gateway_service
from ..message_stats import message_stats

router = APIRouter(prefix="/gateway", tags=["gateway"])


@router.get("/runs/{run_id}/message-stats")
async def get_message_stats(run_id: str):
    service = get_gateway_service()
    try:
        return await asyncio.to_thread(message_stats, service.host.run_store,
                                       getattr(service.host, "ledger_store", None), run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
