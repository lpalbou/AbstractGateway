"""The caller's push devices (framework backlog 1049): the watch and iPhone apps register here.

    PUT    /api/gateway/me/push/devices/{device_id}   register or refresh (at sign-in, daily, new token)
    DELETE /api/gateway/me/push/devices/{device_id}   sign-out
    GET    /api/gateway/me/push/devices               [{device_id, app, name, refreshed_at, last_sent_at, last_error}]

Principal-scoped by construction like `/me/events` (the plane and the principal come from the
request, never from a parameter); a browser session's writes carry the CSRF header (the auth
middleware enforces it). Tokens are stored, never returned. A token registered here leaves every
other registration on this gateway (another account signed in on the same app: the latest sign-in
wins). A registration expires 7 days after its last refresh.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException, Path as PathParam, Request
from pydantic import BaseModel, ConfigDict, Field

from ..push.settings import push_relay_url
from ..push.store import PACKAGED_TOPICS, DeviceStore, principal_key, public_view, write_cursor

router = APIRouter(prefix="/gateway/me/push", tags=["push"])

DEVICE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
INFO_TTL_S = 3600.0
INFO_TIMEOUT_S = 5.0

_info_cache: Dict[str, Tuple[float, Tuple[str, ...]]] = {}
_info_lock = threading.Lock()


class DeviceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., pattern=r"^[0-9A-Fa-f]{64,200}$", description="The APNs device token (hex).")
    topic: str = Field(..., min_length=1, max_length=200, description="The app's bundle id (the APNs topic).")
    env: Literal["production", "sandbox"]
    app: Literal["watch", "phone"]
    reg: str = Field(..., pattern=r"^[0-9A-Fa-f]{16,32}$", description="This sign-in's registration handle (new at every sign-in).")
    name: Optional[str] = Field(None, min_length=1, max_length=60, description="A name the person recognizes (the device's).")


def _context(request: Request) -> Tuple[Any, Any, str]:
    """(service, the caller's stream, principal key)."""
    from ..events_stream import stream_for
    from ..service import get_gateway_service
    from .gateway import _principal_from_request

    principal = _principal_from_request(request)
    svc = get_gateway_service()
    try:
        stream = stream_for(svc.config.data_dir, principal.tenant_id, principal.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return svc, stream, principal_key(stream.tenant, stream.user)


def _store(svc: Any) -> DeviceStore:
    cfg = svc.config
    return DeviceStore(Path(cfg.data_dir), Path(getattr(cfg, "root_data_dir", None) or cfg.data_dir))


def relay_topics(relay: Optional[str]) -> Tuple[str, ...]:
    """The relay's own topics (`GET <relay>/v1/info`), cached an hour; () when it cannot answer.
    Called only for a topic that is not one of the packaged apps, i.e. after a device asked."""
    if not relay:
        return ()
    now = time.monotonic()
    with _info_lock:
        hit = _info_cache.get(relay)
        if hit and now - hit[0] < INFO_TTL_S:
            return hit[1]
    topics: Tuple[str, ...] = ()
    try:
        import httpx

        resp = httpx.get(f"{relay}/v1/info", timeout=INFO_TIMEOUT_S)
        data = resp.json() if resp.status_code == 200 else {}
        if isinstance(data, dict) and isinstance(data.get("topics"), list):
            topics = tuple(str(t) for t in data["topics"] if isinstance(t, str))
    except Exception:  # noqa: BLE001 - an unreachable relay allows only the packaged apps
        topics = ()
    with _info_lock:
        _info_cache[relay] = (now, topics)
    return topics


def relay_state(store: DeviceStore, principal: str) -> str:
    if push_relay_url() is None:
        return "off"
    if any(d.get("relay_unreachable_since") for d in store.active(principal=principal)):
        return "unreachable"
    return "on"


def _is_plane_owner(svc: Any, who: str) -> bool:
    """The dispatcher of a plane serves its owner's stream (the one the runner writes)."""
    from ..events_stream import plane_owner

    try:
        return principal_key(*plane_owner(svc.config)) == who
    except Exception:  # noqa: BLE001
        return False


def _kick(svc: Any) -> None:
    dispatcher = getattr(svc, "push_dispatcher", None)
    if dispatcher is not None:
        dispatcher.kick()


@router.put("/devices/{device_id}")
async def put_my_push_device(
    request: Request,
    body: DeviceBody,
    device_id: str = PathParam(..., pattern=DEVICE_ID_PATTERN),
) -> Dict[str, Any]:
    """Register (or refresh) this device for alerts while its app is closed. Answers
    `{ok, relay: on|off|unreachable}` — `off`: this gateway sends no pushes (`--push-relay off`)."""
    svc, stream, who = _context(request)
    topic = body.topic.strip()
    if topic not in PACKAGED_TOPICS and topic not in await asyncio.to_thread(relay_topics, push_relay_url()):
        raise HTTPException(status_code=422, detail=f"unknown app topic {topic!r}: this gateway pushes only to {', '.join(PACKAGED_TOPICS)} and the relay's own apps")
    store = _store(svc)
    _rec, first = await asyncio.to_thread(
        store.put, principal=who, device_id=device_id, token=body.token, topic=topic, env=body.env, app=body.app,
        reg=body.reg, name=(body.name or "").strip() or None,
    )
    if first and _is_plane_owner(svc, who):
        # The first registration: the dispatcher starts from now, never from history.
        await asyncio.to_thread(lambda: write_cursor(store.plane_dir, int(stream.head())))
    _kick(svc)
    return {"ok": True, "relay": relay_state(store, who)}


@router.delete("/devices/{device_id}")
async def delete_my_push_device(request: Request, device_id: str = PathParam(..., pattern=DEVICE_ID_PATTERN)) -> Dict[str, Any]:
    """Forget this device (sign-out). Idempotent: `{ok, deleted}`."""
    svc, _stream, who = _context(request)
    deleted = await asyncio.to_thread(_store(svc).remove, principal=who, device_id=device_id)
    _kick(svc)
    return {"ok": True, "deleted": bool(deleted)}


@router.get("/devices")
async def list_my_push_devices(request: Request) -> List[Dict[str, Any]]:
    """The caller's registered devices, oldest first. Never the tokens."""
    svc, _stream, who = _context(request)
    rows = await asyncio.to_thread(_store(svc).active, principal=who)
    return [public_view(r) for r in rows]


__all__ = ["router", "relay_topics"]
