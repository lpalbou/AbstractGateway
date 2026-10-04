"""Gateway-owned exposure of Core's serving routes on the Gateway listener.

The OpenAI-compatible API lives at `/v1` (standard OpenAI layout); `/core/v1`
answers 308 to it for one release (deprecated). Configuration, who may
connect and the request log belong to this host; inference and provider
credential policy belong to Core (framework ADR-0033, Core ADR-0004).

Authentication: a caller's API key is their own gateway token (resolved by
the security middleware into `scope["state"]["gateway_principal"]`). The
stored endpoint token is the gateway's internal credential towards Core; a
client presenting it is still accepted for one release (the 0.12.0 key).

Who can connect (`reach`) filters on the client address: the socket peer, or
the first X-Forwarded-For hop when the peer is this machine (a local proxy
such as `tailscale serve`) or when proxies elsewhere are trusted.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse

from abstractruntime.integrations.abstractcore.server_facade import serve_core_request

from . import network_exposure as ne
from .runtime_config import resolve_network_setting, store_lock
from .users import gateway_data_dir_from_env

ACCESS_MODES = ("token", "open")
REACH_MODES = ("machine", "network", "tailnet", "anywhere")
BASE_PATH = "/v1"
LEGACY_PREFIX = "/core"
# The client classes each reach admits (cumulative: a wider reach keeps the narrower ones).
REACH_ADMITS = {
    "machine": frozenset({"machine"}),
    "network": frozenset({"machine", "network"}),
    "tailnet": frozenset({"machine", "network", "tailnet"}),
    "anywhere": frozenset({"machine", "network", "tailnet", "internet"}),
}
REACH_LABELS = {
    "machine": "This machine only",
    "network": "Devices on my network",
    "tailnet": "Tailnet",
    "anywhere": "Anywhere",
}


# Open (no key) mode: anonymous requests run as this account. The built-in guest
# may only use models (no tools, no files or media inputs, no media generation).
GUEST_ACCOUNT = "guest"
GUEST_LABEL = "Guest (models only)"
GUEST_PATHS = frozenset({"/v1/models", "/v1/chat/completions", "/v1/responses", "/v1/embeddings"})
_GUEST_TOOL_FIELDS = ("tools", "tool_choice", "functions", "function_call")
_TEXT_PARTS = frozenset({"text", "input_text", "output_text"})


@dataclass(frozen=True)
class EndpointSettings:
    enabled: bool = False
    access: str = "token"
    reach: str = "machine"
    token: str = field(default="", repr=False)
    open_account: str = GUEST_ACCOUNT


def _settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "config" / "core_endpoint.json"


def read_settings(data_dir: Path) -> EndpointSettings:
    path = _settings_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return EndpointSettings()
    if not isinstance(raw, dict):
        raise ValueError("Invalid Core endpoint settings; restore config/core_endpoint.json")
    # A 0.12.0 file has no `reach`: it served every client the listener reached.
    reach = raw.get("reach", "network")
    open_account = raw.get("open_account", GUEST_ACCOUNT)
    if (not isinstance(open_account, str) or not open_account.strip()
            or type(raw.get("enabled")) is not bool
            or raw.get("access") not in ACCESS_MODES
            or reach not in REACH_MODES
            or not isinstance(raw.get("token"), str)
            or (raw["enabled"] and not raw["token"].strip())):
        raise ValueError("Invalid Core endpoint settings; restore config/core_endpoint.json")
    return EndpointSettings(raw["enabled"], raw["access"], reach, raw["token"], open_account.strip())


def open_account_refusal(account: str, *, tenant_id: str = "default") -> Optional[str]:
    """None when Open-mode requests may run as `account`, else why not (one sentence)."""
    if account == GUEST_ACCOUNT:
        return None
    from .users import GatewayUserRegistry

    rec = GatewayUserRegistry().get_user(account, tenant_id=tenant_id)
    if rec is None:
        return f"There is no account named {account!r} on this gateway."
    if rec.principal_kind == "entity":
        return "Entities can't be chosen: requests without a key run as a person's account or as Guest."
    if "admin" in {str(r).strip() for r in rec.roles}:
        return "Requests without a key never run as an admin: choose Guest or a user account."
    if rec.archived or not rec.enabled:
        return f"{account} is not active."
    if not rec.openai_api_allowed():
        return f"The OpenAI API is off for {account}: turn it on in Accounts first."
    return None


def open_account_options(settings: "EndpointSettings") -> List[Dict[str, Any]]:
    """The accounts Open mode can run as: Guest first, then every human account (admins and
    inactive ones listed as unavailable, with the reason)."""
    from .users import GatewayUserRegistry

    out: List[Dict[str, Any]] = [{"id": GUEST_ACCOUNT, "label": GUEST_LABEL, "available": True,
                                  "selected": settings.open_account == GUEST_ACCOUNT}]
    for rec in sorted(GatewayUserRegistry().list_users(), key=lambda r: r.user_id):
        if rec.principal_kind == "entity" or rec.archived or rec.tenant_id != "default":
            continue
        reason = open_account_refusal(rec.user_id)
        row: Dict[str, Any] = {"id": rec.user_id, "label": rec.user_id, "available": reason is None,
                               "selected": settings.open_account == rec.user_id}
        if reason:
            row["reason"] = reason
        out.append(row)
    if not any(o["selected"] for o in out):
        out.append({"id": settings.open_account, "label": settings.open_account, "available": False, "selected": True,
                    "reason": open_account_refusal(settings.open_account) or ""})
    return out


def anywhere_allowed(data_dir: Path) -> Optional[str]:
    """None when `reach=anywhere` may be chosen, else the reason it may not."""
    net = resolve_network_setting(data_dir)
    if net.get("mode") != "internet" or not net.get("internet_acknowledged"):
        return "Anywhere needs Internet on the Network page first (it asks you to confirm the risks)."
    return None


def change_settings(data_dir: Path, *, enabled=None, access=None, reach=None, open_account=None,
                    rotate=False) -> EndpointSettings:
    with store_lock(data_dir):
        current = read_settings(data_dir)
        updated = replace(current,
                          enabled=current.enabled if enabled is None else enabled,
                          access=current.access if access is None else access,
                          reach=current.reach if reach is None else reach,
                          open_account=current.open_account if open_account is None else str(open_account).strip())
        if open_account is not None:
            reason = open_account_refusal(updated.open_account)
            if reason:
                raise ValueError(reason)
        if updated.access not in ACCESS_MODES or updated.reach not in REACH_MODES:
            raise ValueError("Unknown authentication or reach value")
        if access == "open" and resolve_network_setting(data_dir)["mode"] == "internet":
            raise ValueError("Internet mode requires a key. Keep Protected or change Network to Local network.")
        if reach == "anywhere":
            reason = anywhere_allowed(data_dir)
            if reason:
                raise ValueError(reason)
        if updated.reach == "anywhere" and updated.access == "open":
            raise ValueError("Anywhere needs a key: choose Protected first.")
        if rotate or (updated.enabled and not updated.token):
            updated = replace(updated, token="ac_" + secrets.token_urlsafe(32))
        path = _settings_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".core_endpoint-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(updated), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    if updated.enabled and not current.enabled:
        # Round-5 migration: the "OpenAI API" switch is written for every account that never
        # had it (on for active accounts) the first time the endpoint starts.
        from .users import GatewayUserRegistry

        GatewayUserRegistry().migrate_openai_api_default()
    return updated


_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10",
))
_TAILNET_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("100.64.0.0/10", "fd7a:115c:a1e0::/48"))
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10",
))
_FORWARD_HEADERS = {b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto"}


def _ip(raw: Any):
    try:
        ip = ipaddress.ip_address(str(raw).strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def _header(scope: dict, name: bytes) -> Optional[str]:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def client_address(scope: dict, *, trust_proxy: bool) -> str:
    """The address a request comes from. A proxy on this machine (loopback
    peer) is believed by default; a proxy elsewhere only when trusted."""
    peer = str((scope.get("client") or ("", 0))[0] or "")
    xff = _header(scope, b"x-forwarded-for")
    peer_ip = _ip(peer)
    if xff and (trust_proxy or (peer_ip is not None and peer_ip.is_loopback)):
        first = xff.split(",")[0].strip()
        if first:
            return first
    return peer


def classify_client(scope: dict, *, trust_proxy: bool) -> str:
    """machine | network | tailnet | internet for the request's client."""
    ip = _ip(client_address(scope, trust_proxy=trust_proxy))
    if ip is None:
        return "internet"
    if ip.is_loopback:
        peer = _ip((scope.get("client") or ("", 0))[0])
        forwarded = any(k.lower() in _FORWARD_HEADERS for k, _ in scope.get("headers") or [])
        # A loopback peer that says it forwards without naming the client
        # (no X-Forwarded-For) hides where the request comes from.
        if forwarded and peer is not None and peer.is_loopback and not _header(scope, b"x-forwarded-for"):
            return "internet"
        return "machine"
    if any(ip in n for n in _TAILNET_NETWORKS):
        return "tailnet"
    if any(ip in n for n in _PRIVATE_NETWORKS):
        return "network"
    return "internet"


def open_access_allowed(scope: dict, data_dir: Path) -> bool:
    # Open applies only to direct local/LAN/VPN peers. A reverse proxy can
    # hide a public caller behind a loopback socket, so it always needs a key.
    if resolve_network_setting(data_dir)["mode"] == "internet" or ne.live_reverse_proxy(data_dir).trust_proxy:
        return False
    effective = ne.effective_bind(data_dir)
    if effective.get("mode") == "internet":
        return False
    host = effective.get("bind_host")
    if host and host not in {"0.0.0.0", "::", "localhost"}:
        try:
            if not any(ipaddress.ip_address(host) in network for network in _LOCAL_NETWORKS):
                return False
        except ValueError:
            return False
    if any(k.lower() in _FORWARD_HEADERS for k, _ in scope.get("headers", [])):
        return False
    try:
        peer = ipaddress.ip_address((scope.get("client") or ("", 0))[0])
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        return any(peer in network for network in _LOCAL_NETWORKS)
    except (KeyError, ValueError, TypeError):
        return False


SERVING_ROUTES = {
    "/v1/models": "GET",
    "/v1/chat/completions": "POST",
    "/v1/responses": "POST",
    "/v1/embeddings": "POST",
    "/v1/audio/speech": "POST",
    "/v1/audio/transcriptions": "POST",
    "/v1/audio/translations": "POST",
    "/v1/images/generations": "POST",
    "/v1/images/edits": "POST",
    "/v1/images/variations": "POST",
}


# What this API supports, in the words the console's Docs card and the docs page use.
SUPPORT = {
    "tested": ["GET /v1/models", "GET /v1/models/{id}",
               "POST /v1/chat/completions: streaming, tools and tool_choice, stream_options.include_usage, max_completion_tokens",
               "POST /v1/chat/completions: response_format json_object and json_schema (structured outputs, every provider)",
               "POST /v1/embeddings: float and base64"],
    "served": ["POST /v1/responses", "POST /v1/audio/speech", "POST /v1/audio/transcriptions",
               "POST /v1/audio/translations", "POST /v1/images/generations", "POST /v1/images/edits",
               "POST /v1/images/variations"],
    "not_yet": ["response_format together with tools", "text.format on /v1/responses", "n > 1", "logprobs", "logit_bias",
                "files, batches, assistants, fine-tuning, moderations, realtime"],
}


# ---- In-flight requests: Stop and Restart end them -----------------------

class EndpointStopped(Exception):
    """Raised inside a request whose endpoint was stopped or restarted."""


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: set = set()


def end_inflight_requests() -> int:
    """Signal every in-flight request to end; returns how many were open."""
    with _INFLIGHT_LOCK:
        events = list(_INFLIGHT)
    for ev in events:
        ev.set()
    return len(events)


def inflight_count() -> int:
    with _INFLIGHT_LOCK:
        return len(_INFLIGHT)


def openai_error(status: int, message: str, *, type_: str = "invalid_request_error", param: Optional[str] = None,
                 code: Optional[str] = None, headers: Optional[dict] = None) -> JSONResponse:
    """The OpenAI error envelope: {"error": {"message", "type", "param", "code"}}."""
    return JSONResponse({"error": {"message": message, "type": type_, "param": param, "code": code}},
                        status_code=status, headers=headers)


# ---- OpenAI conformance at the boundary ----------------------------------
#
# Core serves the routes; the gateway keeps the public surface standard:
# request fields Core does not take are mapped or refused (never silently
# ignored when they change the answer), error bodies use the standard
# envelope, and chat streams follow the chunk rules (role on the first
# delta, finish_reason only on the last chunk, usage in its own chunk only
# with stream_options.include_usage).

_BODY_MAX = 64 * 1024 * 1024
# Accepted and ignored: they only label or route the request at OpenAI.
_IGNORED_FIELDS = ("user", "store", "metadata", "service_tier", "parallel_tool_calls", "stream_options",
                   "prediction", "modalities", "safety_identifier", "prompt_cache_key_openai")


class RequestRefused(Exception):
    def __init__(self, message: str, param: Optional[str] = None, code: Optional[str] = "unsupported_parameter",
                 status: int = 400):
        super().__init__(message)
        self.param = param
        self.code = code
        self.status = status


def normalize_chat_request(doc: Dict[str, Any]) -> Dict[str, Any]:
    """A /v1/chat/completions body made fit for Core; raises RequestRefused
    for a parameter whose effect Core cannot honour. Returns {include_usage}."""
    if not isinstance(doc, dict):
        raise RequestRefused("The request body must be a JSON object.", None, None)
    facts = {"include_usage": bool(isinstance(doc.get("stream_options"), dict) and doc["stream_options"].get("include_usage"))}
    model = doc.get("model")
    if not isinstance(model, str) or "/" not in model.strip("/"):
        raise RequestRefused(f"The model {model!r} does not exist: use provider/model, as listed at /v1/models.",
                             "model", "model_not_found", 404)
    if "max_completion_tokens" in doc:
        value = doc.pop("max_completion_tokens")
        if doc.get("max_tokens") is None:
            doc["max_tokens"] = value
    rf = doc.get("response_format")
    if rf is not None:
        # Structured outputs: AbstractCore drives them for every provider (constrained decoding
        # where the provider has it, the schema in the prompt otherwise, and the answer validated
        # against the schema). The shape is checked here so a bad schema never reaches a model.
        from abstractcore.structured.json_schema import ResponseFormatError, parse_response_format

        try:
            kind, _schema, _name = parse_response_format(rf)
        except ResponseFormatError as exc:
            raise RequestRefused(str(exc), exc.param, "invalid_response_format") from None
        if kind == "text":
            doc.pop("response_format")
        elif doc.get("tools"):
            raise RequestRefused("response_format cannot be combined with tools yet: send one or the other.",
                                 "response_format")
    n = doc.get("n")
    if n is not None:
        if n != 1:
            raise RequestRefused("n must be 1: one choice per request.", "n")
        doc.pop("n")
    if doc.get("logprobs"):
        raise RequestRefused("logprobs is not supported yet.", "logprobs")
    doc.pop("logprobs", None)
    doc.pop("top_logprobs", None)
    if doc.get("logit_bias"):
        raise RequestRefused("logit_bias is not supported yet.", "logit_bias")
    doc.pop("logit_bias", None)
    if isinstance(doc.get("stop"), str):
        doc["stop"] = [doc["stop"]]
    # The OpenAI layout: tool calls as structured `tool_calls`, never as a
    # model family's text tags (Core picks tags from the model name otherwise).
    doc.setdefault("agent_format", "openai")
    for key in _IGNORED_FIELDS:
        doc.pop(key, None)
    return facts


_ERROR_TYPES = {400: "invalid_request_error", 401: "invalid_request_error", 403: "invalid_request_error",
                404: "invalid_request_error", 409: "invalid_request_error", 413: "invalid_request_error",
                422: "invalid_request_error", 429: "rate_limit_error"}


def standard_error_body(status: int, raw: bytes) -> Tuple[int, bytes]:
    """Core's error body -> (status, the OpenAI envelope). 422 becomes 400."""
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        doc = {}
    err = doc.get("error") if isinstance(doc, dict) else None
    message, param, code, etype = None, None, None, None
    if isinstance(err, dict):
        message, param, code, etype = err.get("message"), err.get("param"), err.get("code"), err.get("type")
        details = err.get("details")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            first = details[0]
            field = str(first.get("field") or "")
            param = param or (field.split(" -> ")[-1] if field else None)
            message = f"{first.get('message') or message} ({field})" if field else (first.get("message") or message)
    elif isinstance(doc, dict) and "detail" in doc:
        detail = doc["detail"]
        message = detail.get("message") if isinstance(detail, dict) else detail
        code = detail.get("reason_code") if isinstance(detail, dict) else None
    if isinstance(message, str) and message.startswith("{'error'"):
        # Core stringifies an HTTPException whose detail is itself an envelope.
        import ast

        try:
            inner = ast.literal_eval(message).get("error") or {}
            message = inner.get("message") or message
            etype = etype if etype not in (None, "http_error") else inner.get("type")
            code = code or (inner.get("type") if inner.get("type") not in (None, "invalid_request") else None)
        except (ValueError, SyntaxError, AttributeError):
            pass
    if not isinstance(message, str) or not message:
        message = raw.decode("utf-8", "replace")[:2000] if raw else f"HTTP {status}"
    out_status = 400 if status == 422 else status
    known = {"invalid_request_error", "authentication_error", "permission_error", "not_found_error",
             "rate_limit_error", "server_error", "api_error"}
    etype = etype if etype in known else _ERROR_TYPES.get(out_status, "server_error" if out_status >= 500 else "invalid_request_error")
    body = {"error": {"message": message, "type": etype, "param": param if isinstance(param, str) else None,
                      "code": code if isinstance(code, str) else None}}
    return out_status, json.dumps(body).encode("utf-8")


# Request fields Core would honour to re-route a provider or carry a credential.
# At /v1 the model is chosen by `model` alone, through the gateway's own routes
# and stored credentials; a caller never points the gateway somewhere else.
# (A caller's OWN provider key stays possible through the
# X-AbstractCore-Provider-API-Key header: a credential, never a destination.)
ROUTING_FIELDS = frozenset({
    "base_url", "api_base", "api_key", "provider", "provider_hint", "provider_kwargs", "headers",
    "extra_headers", "default_headers", "endpoint", "upstream", "upstream_base_url", "base_url_key",
    "organization", "project",
})


def _capability_refusal(principal) -> Optional[str]:
    """Why a signed-in caller may not use /v1 (its account's "OpenAI API" switch is off), or None.
    The operator's own token (not a registry account) is always allowed."""
    if getattr(principal, "source", "") != "user-registry":
        return None
    from .users import GatewayUserRegistry

    rec = GatewayUserRegistry().get_user(str(principal.user_id), tenant_id=str(principal.tenant_id or "default"))
    if rec is None or rec.openai_api_allowed():
        return None
    return "The OpenAI API is off for your account. An admin can turn it on in Accounts."


def _inactive_account(token: str) -> Optional[str]:
    from .users import GatewayUserRegistry

    rec = GatewayUserRegistry().inactive_record_for(token)
    return rec.user_id if rec is not None else None


def refuse_for_guest(doc: Any) -> None:
    """Guest (Open mode, no key): models only — no tools, no files, images or audio in the input."""
    if not isinstance(doc, dict):
        return
    for name in _GUEST_TOOL_FIELDS:
        if doc.get(name):
            raise RequestRefused(f"Without a key, {name} can't be used (Guest: models only).", name,
                                 "guest_not_allowed", 403)
    turns = doc.get("messages") if isinstance(doc.get("messages"), list) else []
    if isinstance(doc.get("input"), list):
        turns = turns + doc["input"]
    for turn in turns:
        content = turn.get("content") if isinstance(turn, dict) else None
        if isinstance(turn, dict) and turn.get("type") and turn.get("type") not in _TEXT_PARTS | {"message"}:
            content = [turn]
        for part in content if isinstance(content, list) else []:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind is not None and kind not in _TEXT_PARTS:
                raise RequestRefused(f"Without a key, {kind} input can't be sent (Guest: text only).", "messages",
                                     "guest_not_allowed", 403)


def refuse_routing_fields(names) -> None:
    for name in names:
        if str(name).lower() in ROUTING_FIELDS:
            raise RequestRefused(
                f"{name} is not accepted here: the model is chosen by the `model` field only, through this "
                "gateway's providers.", str(name), "unsupported_parameter")


async def _form_field_names(scope, raw: bytes) -> List[str]:
    """The field names of a form body (multipart or urlencoded), parsed by Starlette."""
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": raw, "more_body": False}

    try:
        form = await Request(dict(scope, type="http"), receive).form(max_files=1000, max_fields=10000)
    except Exception:  # noqa: BLE001 - a malformed form is Core's 400 to give
        return []
    try:
        return list(form.keys())
    finally:
        await form.close()


def normalize_embeddings_request(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Core computes floats; base64 (the OpenAI SDK's default) is encoded here."""
    if not isinstance(doc, dict):
        raise RequestRefused("The request body must be a JSON object.", None, None)
    fmt = str(doc.get("encoding_format") or "float").lower()
    if fmt not in ("float", "base64"):
        raise RequestRefused("encoding_format must be float or base64.", "encoding_format", None)
    doc["encoding_format"] = "float"
    doc.pop("user", None)
    return {"base64": fmt == "base64"}


def embeddings_to_base64(raw: bytes) -> bytes:
    import base64
    import struct

    try:
        doc = json.loads(raw)
    except ValueError:
        return raw
    for row in doc.get("data") or [] if isinstance(doc, dict) else []:
        vec = row.get("embedding") if isinstance(row, dict) else None
        if isinstance(vec, list) and all(isinstance(x, (int, float)) for x in vec):
            row["embedding"] = base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")
    return json.dumps(doc).encode("utf-8")


class ChatStreamNormalizer:
    """Rewrites a chat completion SSE stream event by event (see above)."""

    def __init__(self, include_usage: bool, note: Dict[str, Any]):
        self.include_usage = include_usage
        self.note = note
        self.buf = b""
        self.first = True
        self.usage: Optional[Dict[str, Any]] = None
        self.last: Dict[str, Any] = {}

    def _event(self, event: bytes) -> bytes:
        lines = event.split(b"\n")
        data = [ln[5:].strip() for ln in lines if ln.startswith(b"data:")]
        if len(data) != 1:
            return event + b"\n\n"
        payload = data[0]
        if payload == b"[DONE]":
            out = b""
            if self.include_usage and self.usage is not None:
                tail = {"id": self.last.get("id"), "object": "chat.completion.chunk", "created": self.last.get("created"),
                        "model": self.last.get("model"), "choices": [], "usage": self.usage}
                out += b"data: " + json.dumps(tail).encode("utf-8") + b"\n\n"
            return out + b"data: [DONE]\n\n"
        try:
            chunk = json.loads(payload)
        except ValueError:
            return event + b"\n\n"
        if not isinstance(chunk, dict) or chunk.get("object") != "chat.completion.chunk":
            return event + b"\n\n"
        self.last = {k: chunk.get(k) for k in ("id", "created", "model")}
        usage = chunk.pop("usage", None)
        if isinstance(usage, dict):
            self.usage = usage
            self.note["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else None
            if delta is None:
                continue
            if self.first:
                delta.setdefault("role", "assistant")
            # finish_reason belongs to the last chunk only (Core marks every tool delta).
            if delta.get("tool_calls") and choice.get("finish_reason") is not None:
                choice["finish_reason"] = None
        if chunk.get("choices"):
            self.first = False
        return b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"

    def feed(self, body: bytes) -> bytes:
        self.buf += body.replace(b"\r\n", b"\n")
        out = b""
        while b"\n\n" in self.buf:
            event, self.buf = self.buf.split(b"\n\n", 1)
            if event.strip():
                out += self._event(event)
        return out

    def flush(self) -> bytes:
        rest, self.buf = self.buf, b""
        return self._event(rest) if rest.strip() else b""


async def _read_body(receive) -> Tuple[bytes, bool]:
    chunks, size = [], 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return b"".join(chunks), True
        body = message.get("body") or b""
        size += len(body)
        if size > _BODY_MAX:
            raise RequestRefused(f"The request body is larger than {_BODY_MAX // (1024 * 1024)} MB.", None, "request_too_large")
        chunks.append(body)
        if not message.get("more_body"):
            return b"".join(chunks), False


def _standardize_send(send, note: Dict[str, Any], *, chat_stream: Optional[ChatStreamNormalizer], base64_embeddings: bool = False):
    """Wraps `send`: error bodies -> the standard envelope; chat SSE -> normalized
    chunks; embeddings -> base64 when the caller asked for it."""
    st: Dict[str, Any] = {"start": None, "error": False, "sse": False, "buf": b"", "b64": False}

    async def wrapped(message):
        kind = message.get("type")
        if kind == "http.response.start":
            status = int(message.get("status") or 0)
            ctype = b""
            for k, v in message.get("headers") or []:
                if bytes(k).lower() == b"content-type":
                    ctype = bytes(v).lower()
            if status >= 400 and b"text/event-stream" not in ctype:
                st.update(start=message, error=True)
                return
            st["sse"] = b"text/event-stream" in ctype and chat_stream is not None
            if base64_embeddings and b"application/json" in ctype:
                st.update(start=message, b64=True)
                return
            if st["sse"]:
                headers = [(k, v) for k, v in message.get("headers") or [] if bytes(k).lower() != b"content-length"]
                message = dict(message, headers=headers)
            await send(message)
            return
        if kind == "http.response.body":
            body = message.get("body") or b""
            more = bool(message.get("more_body"))
            if st["error"]:
                st["buf"] += body
                if more:
                    return
                status, out = standard_error_body(int(st["start"].get("status") or 500), st["buf"])
                headers = [(k, v) for k, v in st["start"].get("headers") or []
                           if bytes(k).lower() not in (b"content-length", b"content-type")]
                headers += [(b"content-type", b"application/json"), (b"content-length", str(len(out)).encode())]
                await send(dict(st["start"], status=status, headers=headers))
                await send({"type": "http.response.body", "body": out})
                return
            if st["b64"]:
                st["buf"] += body
                if more:
                    return
                out = embeddings_to_base64(st["buf"])
                headers = [(k, v) for k, v in st["start"].get("headers") or [] if bytes(k).lower() != b"content-length"]
                headers.append((b"content-length", str(len(out)).encode()))
                await send(dict(st["start"], headers=headers))
                await send({"type": "http.response.body", "body": out})
                return
            if st["sse"]:
                out = chat_stream.feed(body)
                if not more:
                    out += chat_stream.flush()
                if out or not more:
                    await send({"type": "http.response.body", "body": out, "more_body": more})
                return
        await send(message)

    return wrapped


async def _core_models(scope, settings: EndpointSettings, *, authenticated: bool, allow_open: bool,
                       query: bytes = b"") -> Tuple[int, Any]:
    """GET /v1/models through Core for this caller: (status, parsed body)."""
    body = bytearray()
    status = {"code": 0}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = int(message.get("status") or 0)
        elif message["type"] == "http.response.body":
            body.extend(message.get("body") or b"")

    headers = [(k, v) for k, v in scope.get("headers") or [] if k.lower() != b"authorization"]
    if authenticated:
        headers.append((b"authorization", f"Bearer {settings.token}".encode()))
    sub = dict(scope, method="GET", path="/v1/models", raw_path=b"/v1/models", root_path="", query_string=query, headers=headers)
    await serve_core_request(sub, receive, send, token=settings.token, allow_unauthenticated=allow_open and not authenticated)
    try:
        return status["code"], json.loads(bytes(body))
    except ValueError:
        return status["code"], None


class CoreEndpoint:
    """Direct ASGI delegation preserves streaming and cancellation."""

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        root = str(scope.get("root_path", ""))
        if root and path.startswith(root):
            path = path[len(root):]
        # Mounted at /v1: Starlette leaves the mount prefix in root_path.
        if not path.startswith(BASE_PATH + "/"):
            path = BASE_PATH + path
        state = scope.setdefault("state", {})
        note = state.setdefault("openai_api", {}) if isinstance(state, dict) else {}
        method = scope.get("method")
        retrieve = method == "GET" and path.startswith("/v1/models/") and len(path) > len("/v1/models/")
        if SERVING_ROUTES.get(path) != method and not retrieve:
            return await openai_error(404, f"Invalid URL ({method} {path})")(scope, receive, send)
        try:
            from urllib.parse import parse_qsl

            refuse_routing_fields(k for k, _ in parse_qsl((scope.get("query_string") or b"").decode("latin-1"),
                                                          keep_blank_values=True))
        except RequestRefused as exc:
            return await openai_error(400, str(exc), param=exc.param, code=exc.code)(scope, receive, send)
        data_dir = gateway_data_dir_from_env()
        try:
            settings = await asyncio.to_thread(read_settings, data_dir)
            if not settings.enabled:
                return await openai_error(404, "The OpenAI API is stopped on this gateway.", code="endpoint_stopped")(scope, receive, send)
            trust = bool(ne.live_reverse_proxy(data_dir).trust_proxy)
            kind = classify_client(scope, trust_proxy=trust)
            note["client_class"] = kind
            if kind not in REACH_ADMITS[settings.reach]:
                note["client"] = "refused"
                return await openai_error(
                    403, f"This API accepts {REACH_LABELS[settings.reach].lower()}. "
                         "An admin can change Who can connect on the OpenAI API page.",
                    type_="permission_error", code="client_not_allowed")(scope, receive, send)
            allow_open = settings.access == "open" and await asyncio.to_thread(open_access_allowed, scope, data_dir)
        except (OSError, ValueError):
            return await openai_error(503, "OpenAI API settings are unavailable.", type_="server_error")(scope, receive, send)
        principal = state.get("gateway_principal") if isinstance(state, dict) else None
        authorization = Request(scope).headers.get("authorization")
        scheme, _, credential = (authorization or "").partition(" ")
        legacy = bool(settings.token) and scheme.lower() == "bearer" and hmac.compare_digest(
            credential.strip().encode(), settings.token.encode())
        guest = False
        if principal is not None:
            note["client"] = str(getattr(principal, "user_id", "") or "user")
            refused = await asyncio.to_thread(_capability_refusal, principal)
            if refused:
                return await openai_error(403, refused, type_="permission_error",
                                          code="openai_api_off")(scope, receive, send)
        elif legacy:
            note["client"] = "endpoint key"
        elif allow_open:
            # Open mode: the request runs as the account the admin chose (never an admin),
            # by default the built-in guest that may only use models.
            account = settings.open_account or GUEST_ACCOUNT
            if account == GUEST_ACCOUNT:
                guest = True
                note["client"] = GUEST_ACCOUNT
            else:
                reason = await asyncio.to_thread(open_account_refusal, account)
                if reason:
                    note["client"] = "refused"
                    return await openai_error(
                        403, "Requests without a key are not accepted right now: an admin can choose who they run "
                             "as on the OpenAI API page.", type_="permission_error",
                        code="open_account_unavailable")(scope, receive, send)
                note["client"] = account
                note["run_as"] = account
            if guest and path not in GUEST_PATHS and not retrieve:
                return await openai_error(
                    403, "Without a key, this API answers model requests only (Guest). Send your gateway token "
                         "as the API key for this endpoint.", type_="permission_error",
                    code="guest_not_allowed")(scope, receive, send)
        else:
            note["client"] = "refused"
            inactive = await asyncio.to_thread(_inactive_account, credential.strip()) if credential.strip() else None
            if inactive is not None:
                note["client"] = inactive
                return await openai_error(
                    403, f"The account {inactive} is not active (deactivated or archived), so its key can't use the "
                         "OpenAI API. An admin can turn it back on in Accounts.", type_="permission_error",
                    code="account_inactive")(scope, receive, send)
            message = ("Incorrect API key provided: use your gateway token." if authorization
                       else "You didn't provide an API key: send your gateway token as Authorization: Bearer <token>.")
            return await openai_error(401, message, code="invalid_api_key",
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        # A chosen Open-mode account runs with the gateway's credential, like a keyed caller;
        # the guest stays anonymous towards Core (local models only).
        authenticated = principal is not None or legacy or bool(note.get("run_as"))
        if retrieve:
            from urllib.parse import unquote

            wanted = unquote(path[len("/v1/models/"):])
            status, doc = await _core_models(scope, settings, authenticated=authenticated, allow_open=allow_open)
            if status != 200 or not isinstance(doc, dict):
                return await openai_error(502, "Could not list models.", type_="server_error")(scope, receive, send)
            for row in doc.get("data") or []:
                if isinstance(row, dict) and row.get("id") == wanted:
                    out = {"id": row["id"], "object": "model", "created": row.get("created"), "owned_by": row.get("owned_by")}
                    return await JSONResponse(out)(scope, receive, send)
            return await openai_error(404, f"The model '{wanted}' does not exist.", param="model",
                                      code="model_not_found")(scope, receive, send)
        # Core's security middleware inspects /v1 paths. The caller's own key
        # never reaches Core: an authenticated caller is forwarded with the
        # gateway's credential, an anonymous one with none.
        headers = [(k, v) for k, v in scope.get("headers") or []
                   if k.lower() not in (b"authorization", b"openai-organization", b"openai-project")]
        if authenticated:
            headers.append((b"authorization", f"Bearer {settings.token}".encode()))
        chat_stream = None
        downstream_receive = receive
        ctype = (_header(scope, b"content-type") or "").lower()
        base64_embeddings = False
        if method == "POST":
            try:
                raw, gone = await _read_body(receive)
                if gone:
                    return
                if "application/json" in ctype:
                    try:
                        doc = json.loads(raw) if raw else None
                    except ValueError:
                        return await openai_error(400, "The request body is not valid JSON.")(scope, receive, send)
                    refuse_routing_fields(doc.keys() if isinstance(doc, dict) else ())
                    if guest:
                        refuse_for_guest(doc)
                    if path == "/v1/embeddings":
                        base64_embeddings = normalize_embeddings_request(doc)["base64"]
                    elif path == "/v1/chat/completions":
                        facts = normalize_chat_request(doc)
                        if doc.get("stream"):
                            chat_stream = ChatStreamNormalizer(facts["include_usage"], note)
                    raw = json.dumps(doc).encode("utf-8")
                elif "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
                    if guest:
                        raise RequestRefused("Without a key, files can't be sent (Guest: models only).", None,
                                             "guest_not_allowed", 403)
                    refuse_routing_fields(await _form_field_names(scope, raw))
            except RequestRefused as exc:
                return await openai_error(413 if exc.code == "request_too_large" else exc.status, str(exc),
                                          type_="permission_error" if exc.status == 403 else "invalid_request_error",
                                          param=exc.param, code=exc.code)(scope, receive, send)
            body = raw
            headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode()))
            replayed = {"done": False}

            async def downstream_receive():
                if replayed["done"]:
                    return await receive()
                replayed["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}

        core_scope = dict(scope, path=path, raw_path=path.encode(), root_path="", headers=headers)
        stop = asyncio.Event()
        with _INFLIGHT_LOCK:
            _INFLIGHT.add(stop)
        standard_send = _standardize_send(send, note, chat_stream=chat_stream, base64_embeddings=base64_embeddings)

        async def guarded_send(message):
            if stop.is_set():
                raise EndpointStopped("The OpenAI API was stopped or restarted")
            await standard_send(message)

        async def guarded_receive():
            if stop.is_set():
                return {"type": "http.disconnect"}
            return await downstream_receive()

        try:
            await serve_core_request(core_scope, guarded_receive, guarded_send, token=settings.token,
                                     allow_unauthenticated=allow_open and not authenticated)
        except EndpointStopped:
            note["ended"] = "stopped"
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(stop)


class LegacyCoreRedirect:
    """`/core/v1/...` -> 308 `/v1/...` (method and body preserved). Deprecated."""

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        root = str(scope.get("root_path", ""))
        # Mounted at /core: Starlette leaves the mount prefix in root_path.
        rest = path[len(root):] if root and path.startswith(root) else path
        if not rest.startswith(BASE_PATH + "/"):
            return await openai_error(404, f"Invalid URL ({scope.get('method')} {path})")(scope, receive, send)
        prefix = root[: -len(LEGACY_PREFIX)] if root.endswith(LEGACY_PREFIX) else ""
        location = prefix + rest
        qs = scope.get("query_string") or b""
        if qs:
            location += "?" + qs.decode("latin-1")
        response = JSONResponse({"detail": f"Moved to {BASE_PATH}; /core/v1 is deprecated"}, status_code=308,
                                headers={"Location": location, "Deprecation": "true"})
        await response(scope, receive, send)


# ---- Usage capture for the request log (audit entries, no new store) -----

_CAPTURE_MAX = 2 * 1024 * 1024


def _usage_of(doc: Any) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        return {}
    inner = doc.get("response") if isinstance(doc.get("response"), dict) else None
    out: Dict[str, Any] = {}
    for src in (doc, inner or {}):
        model = src.get("model")
        if isinstance(model, str) and model:
            out["model"] = model
        usage = src.get("usage")
        if isinstance(usage, dict):
            p = usage.get("prompt_tokens", usage.get("input_tokens"))
            c = usage.get("completion_tokens", usage.get("output_tokens"))
            if isinstance(p, int):
                out["prompt_tokens"] = p
            if isinstance(c, int):
                out["completion_tokens"] = c
    return out


# ---- The log's record of each request (redacted, bounded) -----------------
#
# Each audit line keeps the request and the response as they crossed /v1, with
# credentials removed: fields named like a credential, and every occurrence of
# a secret this gateway knows (the caller's key, the internal endpoint key).
# Inline media (data: URLs) is replaced by its type and size. A side larger
# than RECORD_MAX is kept up to that size and marked truncated.

RECORD_MAX = 256 * 1024
REDACTED = "[redacted]"
SECRET_FIELDS = frozenset({
    "api_key", "apikey", "api-key", "x-api-key", "authorization", "proxy-authorization", "token", "access_token",
    "refresh_token", "id_token", "password", "passwd", "secret", "client_secret", "bearer", "credentials",
    "openai_api_key", "anthropic_api_key", "session", "cookie",
})
_DATA_URL_MIN = 256


def redact(value: Any, secrets_: Tuple[str, ...] = ()) -> Any:
    """`value` with credentials removed (see above); never mutates its argument."""
    known = tuple(x for x in secrets_ if isinstance(x, str) and len(x) >= 8)

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: (REDACTED if str(k).lower() in SECRET_FIELDS and v[k] not in (None, "") else walk(v[k]))
                    for k in v}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            if v.startswith("data:") and len(v) > _DATA_URL_MIN:
                head = v[5:].split(",", 1)[0].split(";", 1)[0] or "data"
                return f"[inline {head}, {len(v)} bytes]"
            for secret in known:
                if secret in v:
                    v = v.replace(secret, REDACTED)
            return v
        return v

    return walk(value)


def _bounded(doc: Any, size: int) -> Dict[str, Any]:
    text = json.dumps(doc, ensure_ascii=False)
    if len(text) <= RECORD_MAX:
        return {"body": doc, "bytes": size}
    return {"text": text[:RECORD_MAX], "bytes": size, "truncated": True}


class _StreamAssembler:
    """A chat.completion (or the Responses API's final response) rebuilt from its SSE events."""

    def __init__(self) -> None:
        self.events = 0
        self.base: Dict[str, Any] = {}
        self.content: List[str] = []
        self.reasoning: List[str] = []
        self.tools: Dict[int, Dict[str, Any]] = {}
        self.finish: Optional[str] = None
        self.usage: Optional[Dict[str, Any]] = None
        self.final: Optional[Dict[str, Any]] = None
        self.size = 0

    def event(self, doc: Any) -> None:
        if not isinstance(doc, dict):
            return
        self.events += 1
        if doc.get("type") == "response.completed" and isinstance(doc.get("response"), dict):
            self.final = doc["response"]
            return
        for key in ("id", "created", "model"):
            if doc.get(key) is not None:
                self.base[key] = doc[key]
        if isinstance(doc.get("usage"), dict):
            self.usage = doc["usage"]
        for choice in doc.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if self.size < RECORD_MAX:
                for name, sink in (("content", self.content), ("reasoning_content", self.reasoning),
                                   ("reasoning", self.reasoning)):
                    if isinstance(delta.get(name), str):
                        sink.append(delta[name])
                        self.size += len(delta[name])
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                slot = self.tools.setdefault(int(call.get("index") or 0), {"id": None, "type": "function",
                                                                           "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                slot["function"]["name"] += str(fn.get("name") or "")
                slot["function"]["arguments"] += str(fn.get("arguments") or "")
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def result(self) -> Dict[str, Any]:
        if self.final is not None:
            return {"assembled_from_stream": self.events, **self.final}
        message: Dict[str, Any] = {"role": "assistant", "content": "".join(self.content) or None}
        if self.reasoning:
            message["reasoning"] = "".join(self.reasoning)
        if self.tools:
            message["tool_calls"] = [self.tools[i] for i in sorted(self.tools)]
        out: Dict[str, Any] = {"assembled_from_stream": self.events, "object": "chat.completion", **self.base,
                               "choices": [{"index": 0, "message": message, "finish_reason": self.finish}]}
        if self.usage is not None:
            out["usage"] = self.usage
        return out


class UsageCapture:
    """Tees an OpenAI request/response pair (bounded) into one log summary
    (model, stream, prompt/completion tokens) and, through `record()`, the
    redacted request and response. Streams are read line by line."""

    def __init__(self) -> None:
        self._req = bytearray()
        self._req_size = 0
        self._req_over = False
        self._req_type = ""
        self._resp = bytearray()
        self._resp_size = 0
        self._resp_over = False
        self._resp_type = ""
        self._sse = False
        self._line = b""
        self._stream = _StreamAssembler()
        self.found: Dict[str, Any] = {}

    def request_start(self, content_type: str) -> None:
        self._req_type = str(content_type or "").lower()

    def request_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._req_size += len(chunk)
        if self._req_over:
            return
        if len(self._req) + len(chunk) > _CAPTURE_MAX:
            self._req_over = True
            self._req.clear()
            return
        self._req.extend(chunk)

    def response_start(self, message: dict) -> None:
        for k, v in message.get("headers") or []:
            if bytes(k).lower() == b"content-type":
                self._resp_type = bytes(v).decode("latin-1").lower()
                if b"text/event-stream" in bytes(v).lower():
                    self._sse = True

    def response_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._resp_size += len(chunk)
        if not self._sse:
            if self._resp_over or len(self._resp) + len(chunk) > _CAPTURE_MAX:
                self._resp_over = True
                self._resp.clear()
                return
            self._resp.extend(chunk)
            return
        buf = self._line + chunk
        lines = buf.split(b"\n")
        self._line = lines.pop()[-_CAPTURE_MAX:]
        for line in lines:
            line = line.strip()
            if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                continue
            try:
                doc = json.loads(line[5:].strip())
            except ValueError:
                continue
            self._stream.event(doc)
            if b"usage" in line:
                self.found.update(_usage_of(doc))

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"stream": self._sse}
        try:
            req = json.loads(bytes(self._req)) if self._req else None
        except ValueError:
            req = None
        if isinstance(req, dict) and isinstance(req.get("model"), str):
            out["model"] = req["model"]
        if self._resp and not self._sse:
            try:
                found = _usage_of(json.loads(bytes(self._resp)))
            except ValueError:
                found = {}
            out.update({k: v for k, v in found.items() if k != "model" or "model" not in out})
        out.update({k: v for k, v in self.found.items() if k != "model" or "model" not in out})
        return out

    def _side(self, raw: bytearray, size: int, over: bool, ctype: str, secrets_: Tuple[str, ...]) -> Dict[str, Any]:
        if size == 0:
            return {"bytes": 0}
        if over:
            return {"bytes": size, "omitted": f"larger than {_CAPTURE_MAX // (1024 * 1024)} MB"}
        if "application/json" in ctype or (not ctype and raw[:1] in (b"{", b"[")):
            try:
                return _bounded(redact(json.loads(bytes(raw)), secrets_), size)
            except ValueError:
                pass
        if ctype.startswith("text/") or "json" in ctype:
            text = redact(bytes(raw).decode("utf-8", "replace"), secrets_)
            return {"text": text[:RECORD_MAX], "bytes": size, **({"truncated": True} if len(text) > RECORD_MAX else {})}
        return {"bytes": size, "omitted": f"{ctype.split(';')[0] or 'binary'} body"}

    def record(self, *, secrets: Tuple[str, ...] = ()) -> Dict[str, Any]:
        """{"request": {...}, "response": {...}} as recorded, credentials removed."""
        request = self._side(self._req, self._req_size, self._req_over, self._req_type, secrets)
        if self._sse:
            response = _bounded(redact(self._stream.result(), secrets), self._resp_size)
        else:
            response = self._side(self._resp, self._resp_size, self._resp_over, self._resp_type, secrets)
        return {"request": request, "response": response}


async def listed_text_models(settings: EndpointSettings) -> List[str]:
    """The text model ids `/v1/models?output_type=text` lists (authenticated, as the gateway)."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
             "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 0), "headers": []}
    status, doc = await _core_models(scope, settings, authenticated=True, allow_open=False, query=b"output_type=text")
    if status != 200 or not isinstance(doc, dict):
        return []
    return [str(r["id"]) for r in doc.get("data") or [] if isinstance(r, dict) and r.get("id")]


# ---- Reading the log back (audit files, newest first) --------------------

LOG_MARK = b'"openai_api"'


def _log_lines(data_dir: Optional[Path], byte_budget: int):
    """(doc, openai_api) for every OpenAI API audit line, newest first, within the byte budget."""
    from .account_activity import _lines_backwards, audit_files

    budget = [int(byte_budget)]
    for path in audit_files(data_dir):
        try:
            for line, _start in _lines_backwards(path, budget):
                if LOG_MARK not in line:
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                api = doc.get("openai_api") if isinstance(doc, dict) else None
                if isinstance(api, dict):
                    yield doc, api
        except OSError:
            continue
        if budget[0] <= 0:
            break


def _visible(doc: Dict[str, Any], user_id: Optional[str], tenant_id: Optional[str]) -> bool:
    return user_id is None or (str(doc.get("principal_user_id") or "") == user_id
                               and str(doc.get("principal_tenant_id") or "default") == (tenant_id or "default"))


def _row(doc: Dict[str, Any], api: Dict[str, Any]) -> Dict[str, Any]:
    from .account_activity import observer_path_for

    run_id = str(api.get("run_id") or "") or None
    return {
        "request_id": doc.get("request_id"),
        "ts": doc.get("ts"),
        "client": api.get("client") or "unknown",
        "user_id": doc.get("principal_user_id"),
        "ip": doc.get("ip"),
        "method": doc.get("method"),
        "path": doc.get("path"),
        "model": api.get("model"),
        "prompt_tokens": api.get("prompt_tokens"),
        "completion_tokens": api.get("completion_tokens"),
        "stream": bool(api.get("stream")),
        "duration_ms": doc.get("duration_ms"),
        "status": doc.get("status"),
        "run_id": run_id,
        "observer_path": observer_path_for(run_id) if run_id else None,
        "recorded": isinstance(api.get("request"), dict) or isinstance(api.get("response"), dict),
    }


def recent_requests(*, limit: int = 50, user_id: Optional[str] = None, tenant_id: Optional[str] = None,
                    data_dir: Optional[Path] = None, byte_budget: int = 16 * 1024 * 1024) -> List[Dict[str, Any]]:
    """The OpenAI API rows of the audit log, newest first. `user_id` limits the
    rows to that account's own requests (a non-admin caller)."""
    rows: List[Dict[str, Any]] = []
    for doc, api in _log_lines(data_dir, byte_budget):
        if not _visible(doc, user_id, tenant_id):
            continue
        rows.append(_row(doc, api))
        if len(rows) >= limit:
            break
    return rows


def request_record(request_id: str, *, user_id: Optional[str] = None, tenant_id: Optional[str] = None,
                   data_dir: Optional[Path] = None, byte_budget: int = 64 * 1024 * 1024) -> Optional[Dict[str, Any]]:
    """One log row with its recorded request and response (credentials already
    removed when it was written), or None (unknown id, or not the caller's)."""
    for doc, api in _log_lines(data_dir, byte_budget):
        if str(doc.get("request_id") or "") != request_id:
            continue
        if not _visible(doc, user_id, tenant_id):
            return None
        out = _row(doc, api)
        out["request"] = api.get("request") if isinstance(api.get("request"), dict) else None
        out["response"] = api.get("response") if isinstance(api.get("response"), dict) else None
        out["user_agent"] = doc.get("user_agent")
        out["client_class"] = api.get("client_class")
        return out
    return None


# ---- Check setup ---------------------------------------------------------

async def check_setup(data_dir: Path, *, network: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Plain checks, each {id, ok, text}: settings, Core, listener vs reach, models."""
    checks: List[Dict[str, Any]] = []
    try:
        settings = await asyncio.to_thread(read_settings, data_dir)
        checks.append({"id": "settings", "ok": True, "text": "Settings are readable."})
    except (OSError, ValueError) as exc:
        return [{"id": "settings", "ok": False, "text": str(exc)}]
    checks.append({"id": "running", "ok": settings.enabled,
                   "text": "The API is running." if settings.enabled else "The API is stopped: turn on Endpoint."})
    listener = str((network.get("effective") or {}).get("mode") or "unknown")
    if settings.reach == "machine" or listener in {"lan", "internet"}:
        checks.append({"id": "listener", "ok": True, "text": "The gateway listens where Who can connect needs it."})
    elif settings.reach in {"tailnet", "anywhere"}:
        checks.append({"id": "listener", "ok": None,
                       "text": "The gateway listens on this computer only: other devices reach it only through "
                               "a proxy here (tailscale serve, a tunnel)."})
    else:
        checks.append({"id": "listener", "ok": False,
                       "text": "The gateway listens on this computer only: change it on the Network page."})
    try:
        import importlib

        await asyncio.to_thread(importlib.import_module, "abstractcore.server.app")
        checks.append({"id": "core", "ok": True, "text": "AbstractCore's server is installed."})
    except Exception as exc:  # noqa: BLE001 - reported to the admin verbatim
        checks.append({"id": "core", "ok": False, "text": f"AbstractCore's server cannot start: {type(exc).__name__}: {exc}"})
        return checks
    if settings.enabled:
        count = await asyncio.wait_for(_count_models(settings), timeout=20)
        checks.append({"id": "models", "ok": count > 0,
                       "text": f"{count} model{'s' if count != 1 else ''} listed at /v1/models." if count > 0
                       else "No model listed: connect a provider on the Providers page."})
    return checks


async def _count_models(settings: EndpointSettings) -> int:
    """GET /v1/models through Core, as an authenticated caller."""
    body = bytearray()
    status = {"code": 0}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = int(message.get("status") or 0)
        elif message["type"] == "http.response.body":
            body.extend(message.get("body") or b"")

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/v1/models", "raw_path": b"/v1/models", "root_path": "",
             "query_string": b"", "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 0),
             "headers": [(b"authorization", f"Bearer {settings.token}".encode())]}
    await serve_core_request(scope, receive, send, token=settings.token)
    if status["code"] != 200:
        return 0
    try:
        data = json.loads(bytes(body)).get("data")
    except (ValueError, AttributeError):
        return 0
    return len(data) if isinstance(data, list) else 0
