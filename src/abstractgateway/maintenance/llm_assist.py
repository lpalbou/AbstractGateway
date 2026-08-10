from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


# #[WARNING:TIMEOUT] High safeguard, not a performance knob (ADR-0027 §2/§3):
# 2h, the ADR's recommended global default. Set the store key or the env
# override to `0` for no client timeout at all.
DEFAULT_TRIAGE_LLM_TIMEOUT_S = 7200.0


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return str(v).strip()
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return str(v2).strip()
    return None


def _json_from_text(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None
    # Best-effort: find the first JSON object in the output.
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    blob = m.group(0)
    try:
        obj = json.loads(blob)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _core_maintenance_settings() -> Dict[str, Any]:
    """AbstractCore's stored triage settings, through the one seam."""
    try:
        from ..core_config import core_maintenance_settings

        return core_maintenance_settings()
    except Exception:
        return {}


def load_llm_assist_config() -> Dict[str, Any]:
    """The maintenance-triage LLM settings: environment over AbstractCore's store.

    ONE ASSISTANT, ONE STORE. AbstractCore holds these six knobs in its
    `maintenance` config section. An operator who set the triage model there has
    configured this assistant, so the environment names below are the OVERRIDE
    rung rather than the only rung -- otherwise the same assistant would have to
    be configured twice, once per entry point.
    """

    stored = _core_maintenance_settings()

    base_url = str(stored.get("triage_llm_base_url") or "")
    model = str(stored.get("triage_llm_model") or "")
    use_llm = bool(stored.get("triage_llm_enabled") or False)
    try:
        temperature = float(stored.get("triage_llm_temperature", 0.2))
    except Exception:
        temperature = 0.2
    # #[WARNING:TIMEOUT] Triage LLM client timeout. ADR-0027 §2: LLM calls are
    # a correctness-critical path and must NOT carry a low default. The former
    # 30.0s code default aborted local LM Studio calls during prompt
    # processing alone and surfaced as "LLM assist failed", never as "we cut
    # it off". `0` (or any non-positive value) means NO client timeout.
    # Source of the effective value: AbstractCore `maintenance.
    # triage_llm_timeout_s`, overridden by ABSTRACT_TRIAGE_LLM_TIMEOUT_S /
    # ABSTRACTGATEWAY_TRIAGE_LLM_TIMEOUT_S.
    try:
        timeout_s = float(stored.get("triage_llm_timeout_s", DEFAULT_TRIAGE_LLM_TIMEOUT_S))
    except Exception:
        timeout_s = DEFAULT_TRIAGE_LLM_TIMEOUT_S
    # Output budget: NO code default. The former 800 was an arbitrary literal
    # nobody asked for on a STRUCTURED-output call (a backlog draft carrying a
    # markdown acceptance-criteria checklist) — precisely the shape ADR-0026 §2
    # forbids ("do not set arbitrary output caps by default" on structured
    # output paths). None = the key never rides the wire, so the provider uses
    # the model's own ceiling. An operator who wants a bound sets
    # `maintenance.triage_llm_max_tokens` or ABSTRACT_TRIAGE_LLM_MAX_TOKENS.
    max_tokens: Optional[int] = None
    raw_stored_max = stored.get("triage_llm_max_tokens")
    if raw_stored_max is not None:
        try:
            max_tokens = int(raw_stored_max)
        except Exception:
            max_tokens = None

    enabled = str(_env("ABSTRACT_TRIAGE_LLM", "ABSTRACTGATEWAY_TRIAGE_LLM") or "").strip().lower()
    if enabled:
        use_llm = enabled in {"1", "true", "yes", "on"}

    base_url = _env("ABSTRACT_TRIAGE_LLM_BASE_URL", "ABSTRACTGATEWAY_TRIAGE_LLM_BASE_URL") or base_url
    model = _env("ABSTRACT_TRIAGE_LLM_MODEL", "ABSTRACTGATEWAY_TRIAGE_LLM_MODEL") or model
    # The API key stays environment-only on purpose: AbstractCore's
    # `maintenance` section carries no secret, and this module must not invent
    # a place for one to be persisted.
    api_key = _env("ABSTRACT_TRIAGE_LLM_API_KEY", "ABSTRACTGATEWAY_TRIAGE_LLM_API_KEY") or ""

    temperature_raw = _env("ABSTRACT_TRIAGE_LLM_TEMPERATURE", "ABSTRACTGATEWAY_TRIAGE_LLM_TEMPERATURE")
    if temperature_raw is not None:
        try:
            temperature = float(temperature_raw)
        except Exception:
            pass

    timeout_raw = _env("ABSTRACT_TRIAGE_LLM_TIMEOUT_S", "ABSTRACTGATEWAY_TRIAGE_LLM_TIMEOUT_S")
    if timeout_raw is not None:
        try:
            timeout_s = float(timeout_raw)
        except Exception:
            pass

    max_tokens_raw = _env("ABSTRACT_TRIAGE_LLM_MAX_TOKENS", "ABSTRACTGATEWAY_TRIAGE_LLM_MAX_TOKENS")
    if max_tokens_raw is not None:
        try:
            max_tokens = int(max_tokens_raw)
        except Exception:
            pass

    return {
        "enabled": bool(use_llm),
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "timeout_s": timeout_s,
        "max_tokens": max_tokens,
    }


def llm_assist(
    *,
    normalized_input: Dict[str, Any],
    base_url: str,
    model: str,
    api_key: str = "",
    temperature: float = 0.2,
    # #[WARNING:TIMEOUT] High safeguard (ADR-0027 §2); 0/None = no client timeout.
    timeout_s: Optional[float] = DEFAULT_TRIAGE_LLM_TIMEOUT_S,
    # None = no output cap on the wire (ADR-0026 §2 — this is a structured
    # output call). An explicit operator value is honored verbatim.
    max_tokens: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not str(base_url or "").strip():
        return None, "LLM assist base_url is missing"
    if not str(model or "").strip():
        return None, "LLM assist model is missing"

    url = str(base_url).rstrip("/")
    # LMStudio commonly exposes `/v1`; accept both `/v1` and non-versioned roots.
    if not url.endswith("/v1"):
        url = url + "/v1"
    endpoint = url + "/chat/completions"

    # Prompt injection surface is reduced by feeding normalized JSON only.
    system = (
        "You are a maintenance assistant for an open source monorepo.\n"
        "Given a normalized bug/feature report, propose a backlog item draft.\n"
        "Output ONLY valid JSON with keys:\n"
        '- "backlog_title" (string)\n'
        '- "packages" (comma-separated string; prefer one)\n'
        '- "acceptance_criteria" (markdown checklist string, each line starts with "- [ ]")\n'
        '- "notes" (string, optional)\n'
        "Do not include code fences.\n"
    )
    user = json.dumps(normalized_input, ensure_ascii=False, indent=2, sort_keys=True)
    # #[WARNING:TRUNCATION] Input guard on the normalized report payload.
    # Lossy and explicitly marked in-band (ADR-0026 §1) so the model — and any
    # human reading the draft — can see the report was cut here, in
    # abstractgateway.maintenance.llm_assist, and not upstream.
    if len(user) > 25_000:
        user = user[:25_000] + "\n…(truncated by abstractgateway.maintenance.llm_assist at 25000 chars)…\n"

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
    }
    # Only an ASKED-FOR output cap rides the wire (ADR-0026 §2). Absent =>
    # the provider uses the model's own ceiling instead of an invented 800.
    if max_tokens is not None and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            **({"authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )

    # #[WARNING:TIMEOUT] `None`/`<=0` means NO client timeout (ADR-0027 §2 for
    # local providers). A timeout that DOES fire is reported with its duration
    # and this module's name — never as an opaque failure (ADR-0027 §1).
    try:
        eff_timeout = None if timeout_s is None or float(timeout_s) <= 0 else float(timeout_s)
    except (TypeError, ValueError):
        eff_timeout = DEFAULT_TRIAGE_LLM_TIMEOUT_S

    try:
        with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(e)
        return None, f"HTTP error from LLM endpoint: {e.code} {detail}"
    except TimeoutError as e:
        return None, (
            f"#[WARNING:TIMEOUT] abstractgateway.maintenance.llm_assist timed out after "
            f"{eff_timeout}s calling {endpoint} (configure via maintenance.triage_llm_timeout_s "
            f"or ABSTRACT_TRIAGE_LLM_TIMEOUT_S; 0 = no client timeout): {e}"
        )
    except Exception as e:
        if isinstance(getattr(e, "reason", None), TimeoutError) or "timed out" in str(e).lower():
            return None, (
                f"#[WARNING:TIMEOUT] abstractgateway.maintenance.llm_assist timed out after "
                f"{eff_timeout}s calling {endpoint} (configure via maintenance.triage_llm_timeout_s "
                f"or ABSTRACT_TRIAGE_LLM_TIMEOUT_S; 0 = no client timeout): {e}"
            )
        return None, str(e)

    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        return None, "Invalid LLM response (expected JSON object)"

    # OpenAI-compatible response shape.
    content = ""
    finish_reason = ""
    try:
        choices = obj.get("choices") or []
        if isinstance(choices, list) and choices:
            finish_reason = str((choices[0] or {}).get("finish_reason") or "") if isinstance(choices[0], dict) else ""
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = str(msg.get("content") or "")
    except Exception:
        content = ""

    # #[WARNING:TRUNCATION] Provider-side truncation is a CONTRACT VIOLATION on
    # a structured-output call (ADR-0026 §2) and must never be reported as a
    # parse failure. Before this, a `finish_reason=length` response produced
    # "LLM did not return parseable JSON" — the debugging dead-end §1 names,
    # because the JSON was fine until the cap cut it. The message names the
    # responsible component AND the configured source of the cap (§1
    # attribution requirement).
    if str(finish_reason).strip().lower() == "length":
        cap_txt = (
            f"max_tokens={int(max_tokens)} (set via maintenance.triage_llm_max_tokens "
            f"or ABSTRACT_TRIAGE_LLM_MAX_TOKENS)"
            if max_tokens is not None and int(max_tokens) > 0
            else "no max_tokens was sent, so the MODEL's own output ceiling was reached"
        )
        return None, (
            "#[WARNING:TRUNCATION] abstractgateway.maintenance.llm_assist: the model "
            f"stopped at the output budget (finish_reason=length) — {cap_txt}. The draft "
            f"is INCOMPLETE ({len(content)} chars received); raise the budget rather than "
            "trusting this output."
        )

    parsed = _json_from_text(content) if content else None
    if parsed is None:
        return None, "LLM did not return parseable JSON"
    return parsed, None
