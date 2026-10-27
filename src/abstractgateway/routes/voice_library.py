"""The voice library: languages, cloned voices, cloning, voice design, the capability catalog.

`/api/gateway/voice/...`, through AbstractRuntime's voice library facade
(`get_abstractcore_voice_facade`), which runs AbstractCore's voice capability in the gateway's
process or calls a remote AbstractCore server, the same way synthesis does:

    GET    /voice/languages?kind=&provider=        languages each voice engine accepts
    GET    /voice/clones?provider=&model=&language= cloned voices (designed and built-in included)
    GET    /voice/clones/{voice_id}                 one cloned voice
    POST   /voice/clones                            a voice from a recording (multipart)    admin
    PATCH  /voice/clones/{voice_id}                 rename / replace the transcript         admin
    DELETE /voice/clones/{voice_id}                 delete (built-in voices are read-only)  admin
    POST   /voice/design                            a voice from a written description      admin
    POST   /voice/clones/{voice_id}/deploy          share one of my voices with everyone    admin only
    GET    /voice/compatibility                     which engine supports which feature, and how

Whose voices (backlog 0942): in multi-user mode each account keeps the voices it clones or
designs in its own store (`<account plane>/voices`), in front of the machine's shared store, the
library: the built-in voices and every voice the administrator creates. An account lists and
speaks with its own voices and the library's, and creates, renames and deletes its own; library
voices are read-only to it (422). The administrator's plane is the shared store itself, so the
voices the administrator creates are everyone's. Where there is no personal store (single-user
mode, an AbstractCore server doing the voice work, an AbstractVoice before 0.19.0) the store is
shared and changes stay an administrator's act (403 otherwise). A refusal keeps the facade's
status (404 unknown voice, 422 refused input, 501 engine runtime missing, 502 remote AbstractCore
failure).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from ..work_pools import off_loop

router = APIRouter(prefix="/gateway", tags=["voice"])

# A reference recording is a few seconds to a minute of speech; 25 MB covers a minute of
# uncompressed 48 kHz stereo with room to spare.
MAX_REFERENCE_AUDIO_BYTES = 25 * 1024 * 1024


class ClonedVoiceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, description="New display name.")
    reference_text: Optional[str] = Field(default=None, description="New transcript of the reference recording.")


class VoiceDesignRequest(BaseModel):
    description: str = Field(..., description="The voice in words, e.g. 'a calm, warm low female voice'.")
    name: Optional[str] = Field(default=None, description="Display name of the new voice.")
    language: Optional[str] = Field(default=None, description="Language of the reference the voice is built from (ISO code).")
    text: Optional[str] = Field(default=None, description="Sentence the reference is rendered with (default: one per language).")


def _require_change_rights(request: Request) -> None:
    """An administrator, or an account whose runtime keeps its own voice store."""
    from .gateway import _gateway_runtime, _principal_from_request

    principal = _principal_from_request(request)
    if principal.is_admin():
        return
    runtime, _err = _gateway_runtime()
    client = getattr(runtime, "_abstractcore_llm_client", None) if runtime is not None else None
    if str((getattr(client, "_llm_kwargs", None) or {}).get("voice_store_dir") or "").strip():
        return
    raise HTTPException(
        status_code=403,
        detail="On this gateway every account shares one voice store, so creating, changing or deleting a voice is an administrator's act.",
    )


def _facade() -> Any:
    from .gateway import _gateway_runtime

    runtime, err = _gateway_runtime()
    if err or runtime is None:
        raise HTTPException(status_code=503, detail=err or "Gateway runtime is unavailable.")
    try:
        from abstractruntime.integrations.abstractcore import get_abstractcore_voice_facade
    except ImportError as e:  # an AbstractRuntime older than the floor in pyproject.toml
        raise HTTPException(status_code=501, detail=f"The voice library needs a newer AbstractRuntime: {e}") from e
    try:
        return get_abstractcore_voice_facade(runtime)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Gateway runtime is not wired to AbstractCore: {e}") from e


def _keys(request: Request) -> Dict[str, Any]:
    from .gateway import _request_provider_api_key, _voice_openai_api_key

    out: Dict[str, Any] = {"voice_openai_api_key": _voice_openai_api_key()}
    key = _request_provider_api_key(request)
    if key:
        out["provider_api_key"] = key
    return out


async def _run(pool: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    from abstractruntime.integrations.abstractcore import VoiceLibraryError

    try:
        return await off_loop(pool, lambda: fn(*args, **kwargs))
    except VoiceLibraryError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


@router.get("/voice/languages")
async def voice_languages(
    request: Request,
    kind: Optional[str] = Query(default=None, description="`tts`, `stt` or `cloning`; all three when omitted."),
    provider: Optional[str] = Query(default=None, description="One engine, e.g. `qwen3-tts`."),
) -> Dict[str, Any]:
    """`{"languages": {kind: {provider: {provider, languages, open, declared}}}}`, read without
    loading a model. `open`: a remote engine that decides its own languages."""
    facade = _facade()
    return await _run("watchers", facade.list_languages, kind=kind, provider=provider, **_keys(request))


@router.get("/voice/clones")
async def voice_clones(
    request: Request,
    provider: Optional[str] = Query(default=None, description="Cloning engine, e.g. `qwen3-tts`."),
    model: Optional[str] = Query(default=None, description="Model filter."),
    language: Optional[str] = Query(default=None, description="Keep the voices that can speak it; native speakers first."),
) -> Dict[str, Any]:
    """`{"voices": [...], "count": n}`; designed voices carry `tags.designed`, built-in ones `tags.builtin`."""
    facade = _facade()
    return await _run("watchers", facade.list_cloned_voices, provider=provider, model=model, language=language, **_keys(request))


@router.get("/voice/clones/{voice_id}")
async def voice_clone_get(voice_id: str, request: Request) -> Dict[str, Any]:
    """One cloned voice's record (`voice_id`, `name`, `engine`, `reference_text`, `meta`)."""
    facade = _facade()
    return await _run("light", facade.get_cloned_voice, voice_id, **_keys(request))


@router.post("/voice/clones")
async def voice_clone_create(
    request: Request,
    file: UploadFile = File(..., description="Reference recording (WAV, FLAC, OGG, MP3, M4A, WEBM)."),
    name: Optional[str] = Form(default=None, description="Display name."),
    reference_text: Optional[str] = Form(default=None, description="Transcript of the recording (transcribed when omitted, where the engine can)."),
    provider: Optional[str] = Form(default=None, description="Cloning engine, e.g. `qwen3-tts`, `omnivoice`."),
    model: Optional[str] = Form(default=None, description="Cloning model."),
    language: Optional[str] = Form(default=None, description="The recording's language (ISO 639-1); identified from the transcript when omitted."),
) -> Dict[str, Any]:
    """Create a cloned voice from a recording; answers its stored record with `voice_id`."""
    _require_change_rights(request)
    audio = await file.read(MAX_REFERENCE_AUDIO_BYTES + 1)
    if not audio:
        raise HTTPException(status_code=422, detail="The reference recording is empty.")
    if len(audio) > MAX_REFERENCE_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail=f"The reference recording is larger than {MAX_REFERENCE_AUDIO_BYTES // (1024 * 1024)} MB.")
    facade = _facade()
    return await _run(
        "heavy",
        facade.clone_voice,
        audio,
        filename=file.filename or None,
        content_type=file.content_type or None,
        name=name,
        reference_text=reference_text,
        provider=provider,
        model=model,
        language=language,
        **_keys(request),
    )


@router.patch("/voice/clones/{voice_id}")
async def voice_clone_update(voice_id: str, body: ClonedVoiceUpdate, request: Request) -> Dict[str, Any]:
    """Rename and/or replace the transcript; both are checked before either is applied."""
    _require_change_rights(request)
    facade = _facade()
    return await _run("light", facade.update_cloned_voice, voice_id, name=body.name, reference_text=body.reference_text, **_keys(request))


@router.delete("/voice/clones/{voice_id}")
async def voice_clone_delete(voice_id: str, request: Request) -> Dict[str, Any]:
    """Delete a cloned voice and its reference audio. Built-in voices are read-only (422)."""
    _require_change_rights(request)
    facade = _facade()
    return await _run("light", facade.delete_cloned_voice, voice_id, **_keys(request))


@router.post("/voice/design")
async def voice_design(body: VoiceDesignRequest, request: Request) -> Dict[str, Any]:
    """Create a voice from a written description (Qwen3-TTS VoiceDesign, cloned with Qwen3-TTS
    Base): stored as a cloned voice, used with `voice=<voice_id>`. Needs `abstractvoice[qwen3-tts]`
    and `abstractvoice[stt]` where the voice capability runs (501 otherwise)."""
    _require_change_rights(request)
    if not body.description.strip():
        raise HTTPException(status_code=422, detail="`description` must not be empty.")
    facade = _facade()
    return await _run(
        "heavy", facade.design_voice, body.description, name=body.name, language=body.language, text=body.text, **_keys(request)
    )


@router.post("/voice/clones/{voice_id}/deploy")
async def voice_clone_deploy(voice_id: str, request: Request) -> Dict[str, Any]:
    """Share one of the caller's voices with everyone (administrators): copy it into the
    library, the machine's shared voice store every account lists and speaks with. An
    administrator whose plane IS the library (the default administrator) already shares
    every voice they create; the answer then says so (`already_shared`)."""
    from .gateway import _gateway_runtime, _require_admin_principal

    _require_admin_principal(request)
    facade = _facade()
    runtime, _err = _gateway_runtime()
    client = getattr(runtime, "_abstractcore_llm_client", None) if runtime is not None else None
    if not str((getattr(client, "_llm_kwargs", None) or {}).get("voice_store_dir") or "").strip():
        record = await _run("light", facade.get_cloned_voice, voice_id, **_keys(request))
        return {"ok": True, "already_shared": True, **record}
    return await _run("light", facade.deploy_voice_to_library, voice_id, **_keys(request))


@router.get("/voice/compatibility")
async def voice_compatibility(request: Request) -> Dict[str, Any]:
    """The capability catalog: per kind (tts, stt, cloning), provider, model and surface, the
    support of each feature (`native`, `emulated`, `conditional`, `unsupported`), e.g. which
    engines honour `speed` or `one_shot`."""
    facade = _facade()
    return await _run("watchers", facade.compatibility_catalog, **_keys(request))
