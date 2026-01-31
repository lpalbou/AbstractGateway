from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


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


def load_llm_assist_config() -> Dict[str, Any]:
    """Load maintenance LLM config (best-effort).

    Precedence:
    1) env vars (override)
    2) AbstractCore config system (if available)

    This keeps triage configurable while remaining dependency-light when AbstractCore
    isn't installed in a runner-only deployment.
    """

    base_url = ""
    model = ""
    temperature = 0.2
    timeout_s = 30.0
    max_tokens = 800
    use_llm = False

    # AbstractCore config (best-effort).
    try:
        from abstractcore.config.manager import get_config_manager  # type: ignore

        cfg = get_config_manager().config
        m = getattr(cfg, "maintenance", None)
        if m is not None:
            use_llm = bool(getattr(m, "triage_llm_enabled", False))
            base_url = str(getattr(m, "triage_llm_base_url", "") or "")
            model = str(getattr(m, "triage_llm_model", "") or "")
            try:
                temperature = float(getattr(m, "triage_llm_temperature", 0.2))
            except Exception:
                temperature = 0.2
            try:
                timeout_s = float(getattr(m, "triage_llm_timeout_s", 30.0))
            except Exception:
                timeout_s = 30.0
            try:
                max_tokens = int(getattr(m, "triage_llm_max_tokens", 800))
            except Exception:
                max_tokens = 800
    except Exception:
        pass

    # Env overrides.
    enabled = str(_env("ABSTRACT_TRIAGE_LLM", "ABSTRACTGATEWAY_TRIAGE_LLM") or "").strip().lower()
    if enabled:
        use_llm = enabled in {"1", "true", "yes", "on"}

    base_url = _env("ABSTRACT_TRIAGE_LLM_BASE_URL", "ABSTRACTGATEWAY_TRIAGE_LLM_BASE_URL") or base_url
    model = _env("ABSTRACT_TRIAGE_LLM_MODEL", "ABSTRACTGATEWAY_TRIAGE_LLM_MODEL") or model
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
    timeout_s: float = 30.0,
    max_tokens: int = 800,
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
    # Bound payload size to avoid accidental huge requests.
    if len(user) > 25_000:
        user = user[:25_000] + "\n…(truncated)…\n"

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            **({"authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(e)
        return None, f"HTTP error from LLM endpoint: {e.code} {detail}"
    except Exception as e:
        return None, str(e)

    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        return None, "Invalid LLM response (expected JSON object)"

    # OpenAI-compatible response shape.
    content = ""
    try:
        choices = obj.get("choices") or []
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = str(msg.get("content") or "")
    except Exception:
        content = ""

    parsed = _json_from_text(content) if content else None
    if parsed is None:
        return None, "LLM did not return parseable JSON"
    return parsed, None
