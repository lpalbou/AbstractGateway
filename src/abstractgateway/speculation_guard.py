"""MTP backstop: a saved MTP request the selected model cannot honour never fails a run.

Operator rule (2026-11-07): "MTP should only ever be selected for MTP-capable models" and "runs
should not fail". The apps reset MTP when the model changes, but the gateway also runs what was
saved earlier: a flow's Agent/LLM node, an automation's `_runtime.speculation`, an entity mind.
A saved "MTP depth 2, require acceleration" paired with a model that has no MTP made AbstractCore
refuse every call (`SpeculationUnavailableError`), so every run of that flow/automation/entity
failed.

Before each model call the gateway asks the execution host's own capability card
(`get_execution_capabilities(model, provider)["speculation"]["supported"]`, the same card the
console and the kit pickers read; structured data, never the model name). When the card says
`supported: false`, the speculation request is removed from the call (the step runs without MTP)
and the call's result metadata says so in words (`metadata.speculation_dropped`), which the run
ledger keeps with the step. Anything else is left exactly as asked: "Off" (`false`), no request,
a supported model, or a card the host could not produce (unknown is not "unsupported").
"""
from __future__ import annotations

import copy
import dataclasses
import logging
from dataclasses import replace
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: `metadata` key on a model call's result when the gateway removed its MTP request.
DROPPED_KEY = "speculation_dropped"
_WRAPPED_MARK = "_gateway_speculation_guard"


def requests_mtp(value: Any) -> bool:
    """True when `value` asks for speculation (the runtime's own wire: `true` or a mode mapping)."""
    if value is True:
        return True
    if isinstance(value, dict):
        if value.get("enabled") is False:
            return False
        mode = str(value.get("mode") or "native_mtp").strip().lower()
        return mode not in {"off", "none", "disabled"}
    return False


def _resolved_provider(client: Any, provider: Optional[str]) -> Optional[str]:
    """An `endpoint:<id>` provider names a connection; the card is per provider FAMILY."""
    name = str(provider or "").strip()
    if not name:
        return None
    resolver = getattr(client, "resolve_provider_endpoint_profile", None)
    if not callable(resolver):
        return name
    try:
        resolved = resolver(name)
    except Exception:  # noqa: BLE001 - the call itself reports a broken profile
        return name
    if isinstance(resolved, dict):
        family = str(resolved.get("provider") or resolved.get("provider_family") or "").strip()
        if family:
            return family
    return name


def unsupported_card(client: Any, *, provider: Optional[str], model: Optional[str]) -> Optional[Dict[str, Any]]:
    """The model's speculation card when it says MTP is NOT supported, else None.

    `provider`/`model` None mean the client's own defaults (the client resolves them, as its
    `generate` does). A card that cannot be read returns None: the request is left alone."""
    query = getattr(client, "get_execution_capabilities", None)
    if not callable(query):
        return None
    try:
        card = query(model_name=(str(model).strip() or None) if model else None,
                     provider=_resolved_provider(client, provider))
    except Exception as exc:  # noqa: BLE001 - discovery must never take a call down
        logger.debug("speculation guard: capability card unavailable (%s)", exc)
        return None
    spec = card.get("speculation") if isinstance(card, dict) else None
    if isinstance(spec, dict) and spec.get("supported") is False:
        out = dict(spec)
        out.setdefault("model", card.get("model"))
        out.setdefault("provider", card.get("provider"))
        return out
    return None


def drop_note(requested: Any, card: Dict[str, Any]) -> Dict[str, Any]:
    model = str(card.get("model") or "").strip() or "the selected model"
    reason_text = str(card.get("message") or "").strip()
    message = f"MTP was not used: {model} does not support MTP, so this ran without it."
    if reason_text:
        message = f"{message} {reason_text}"
    return {
        "requested": requested,
        "used": False,
        "provider": card.get("provider"),
        "model": card.get("model"),
        "reason": card.get("reason") or "model_has_no_mtp",
        "message": message,
    }


def drop_unsupported_speculation(
    client: Any, params: Dict[str, Any], *, provider: Optional[str], model: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Remove `params["speculation"]` in place when the target model has no MTP; the note, or None."""
    requested = params.get("speculation")
    if not requests_mtp(requested):
        return None
    card = unsupported_card(client, provider=provider, model=model)
    if card is None:
        return None
    params.pop("speculation", None)
    note = drop_note(requested, card)
    logger.warning("speculation guard: %s", note["message"])
    return note


def attach_note(result: Any, note: Optional[Dict[str, Any]]) -> Any:
    """Record the note on a call result (a dict, or an object with `metadata`)."""
    if note is None or result is None:
        return result
    if isinstance(result, dict):
        meta = result.get("metadata")
        result["metadata"] = {**(meta if isinstance(meta, dict) else {}), DROPPED_KEY: note}
        return result
    if hasattr(result, "metadata"):
        meta = getattr(result, "metadata", None)
        try:
            result.metadata = {**(meta if isinstance(meta, dict) else {}), DROPPED_KEY: note}
        except Exception:  # noqa: BLE001 - a read-only result keeps its own metadata
            pass
    return result


def _run_level_speculation(run: Any) -> Any:
    """The run's own MTP request (`_runtime.speculation`: automations, `/runs/start`)."""
    run_vars = getattr(run, "vars", None)
    runtime_ns = run_vars.get("_runtime") if isinstance(run_vars, dict) else None
    return runtime_ns.get("speculation") if isinstance(runtime_ns, dict) else None


def _without_run_level_speculation(run: Any) -> Any:
    """A copy of `run` whose `_runtime` has no `speculation`, for one call.

    AbstractRuntime's LLM_CALL handler fills a call's missing `params.speculation` from the run's
    `_runtime.speculation` (`_maybe_inject_runtime_speculation`, for the call and for its
    structured-output retries) and only reads `run.vars` there; handing it this copy is how the
    call runs with no MTP request at all, rather than an explicit Off. The run itself (and its
    saved `_runtime`) is untouched."""
    run_vars = dict(getattr(run, "vars", None) or {})
    runtime_ns = {k: v for k, v in dict(run_vars.get("_runtime") or {}).items() if k != "speculation"}
    run_vars["_runtime"] = runtime_ns
    if dataclasses.is_dataclass(run) and not isinstance(run, type):
        return dataclasses.replace(run, vars=run_vars)
    out = copy.copy(run)
    out.vars = run_vars
    return out


def wrap_llm_call_handler(handler: Any, *, client: Any) -> Any:
    """An LLM_CALL handler that drops an unsupported MTP request before `handler` runs it: the
    step's own `params.speculation`, or the run-level `_runtime.speculation` the handler would
    otherwise apply to a step that names none."""
    if getattr(handler, _WRAPPED_MARK, False):
        return handler

    def guarded(run: Any, effect: Any, default_next_node: Any = None) -> Any:
        payload = getattr(effect, "payload", None)
        if not isinstance(payload, dict):
            return handler(run, effect, default_next_node)
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        explicit = params.get("speculation")
        run_level = _run_level_speculation(run)
        # The handler's own precedence: a step's request (Off included) wins over the run's.
        requested = explicit if explicit is not None else run_level
        if not requests_mtp(requested):
            return handler(run, effect, default_next_node)
        provider = payload.get("provider") or params.get("_provider")
        model = payload.get("model") or params.get("_model")
        card = unsupported_card(client, provider=provider, model=model)
        if card is None:
            return handler(run, effect, default_next_node)
        note = drop_note(requested, card)
        logger.warning("speculation guard: %s", note["message"])
        if "speculation" in params:
            new_params = dict(params)
            new_params.pop("speculation", None)
            effect = replace(effect, payload={**payload, "params": new_params})
        if requests_mtp(run_level):
            run = _without_run_level_speculation(run)
        outcome = handler(run, effect, default_next_node)
        attach_note(getattr(outcome, "result", None), note)
        return outcome

    setattr(guarded, _WRAPPED_MARK, True)
    return guarded


def install_speculation_guard(runtime: Any) -> bool:
    """Guard the runtime's LLM_CALL handler (every flow, agent, automation and entity run on it).

    Reads AbstractRuntime attributes `_handlers` and `_abstractcore_llm_client` (the same ones
    `model_host.install_runtime_client` relies on). Returns False when the runtime has no LLM
    handler (a tools-only host): there is nothing to guard."""
    from abstractruntime.core.models import EffectType

    handlers = getattr(runtime, "_handlers", None)
    if not isinstance(handlers, dict):
        raise TypeError("speculation guard: this AbstractRuntime keeps its effect handlers differently")
    handler = handlers.get(EffectType.LLM_CALL)
    client = getattr(runtime, "_abstractcore_llm_client", None)
    if handler is None or client is None:
        return False
    handlers[EffectType.LLM_CALL] = wrap_llm_call_handler(handler, client=client)
    return True
