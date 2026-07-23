"""Backend-direct LLM synthesis for grounded audit brief framing.

This module intentionally performs ungrounded chat completion only: no web
search tools, no retrieval, no audit recomputation. Callers supply the full
evidence block and must validate grounding before exposing any text.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

import httpx

from config.settings import settings
from services import vertex_gemini


class LLMSynthesisError(RuntimeError):
    """Base typed failure for strategic synthesis providers."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class MissingLLMKeyError(LLMSynthesisError):
    """Raised when the selected provider has no configured API key."""


class LLMSynthesisHTTPError(LLMSynthesisError):
    """Raised on provider HTTP or transport failures."""


_PROVIDER_ALIASES = {
    "deepseek": "deepseek",
    "openai": "openai",
    "chatgpt": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
}
_DEFAULT_TIMEOUT_S = 30.0


def normalize_provider(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    canonical = _PROVIDER_ALIASES.get(normalized)
    if not canonical:
        raise LLMSynthesisError(
            f"Unsupported synthesis provider: {provider}",
            provider=normalized or "unknown",
        )
    return canonical


def default_model_for_provider(provider: str) -> str:
    canonical = normalize_provider(provider)
    if canonical == "deepseek":
        return settings.deepseek_model
    if canonical == "openai":
        return settings.openai_model
    if canonical == "anthropic":
        return settings.anthropic_model
    if canonical == "gemini":
        return settings.gemini_synthesis_model
    raise LLMSynthesisError(
        f"Unsupported synthesis provider: {provider}",
        provider=canonical,
    )


def configured_key_for_provider(provider: str) -> Optional[str]:
    canonical = normalize_provider(provider)
    if canonical == "deepseek":
        return settings.deepseek_api_key
    if canonical == "openai":
        return settings.openai_api_key
    if canonical == "anthropic":
        return settings.anthropic_api_key
    if canonical == "gemini":
        return settings.gemini_api_key
    return None


async def synthesize(
    *,
    system: str,
    user: str,
    provider: str,
    model: str,
    max_tokens: int = 1200,
    response_schema: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Ungrounded JSON synthesis. `response_schema` (W3b), when supplied, is a
    JSON-Schema the provider enforces at the API layer so the SHAPE is
    guaranteed, not parsed-and-hoped: Gemini `responseSchema`, OpenAI
    `response_format: json_schema`. DeepSeek/Anthropic have no native schema
    mode, so there the schema is advisory (prompt + downstream validation);
    pass a Gemini/OpenAI-compatible schema (OpenAPI subset, no
    additionalProperties) when you rely on enforcement."""
    canonical = normalize_provider(provider)
    selected_model = str(model or default_model_for_provider(canonical)).strip()
    if not selected_model:
        raise LLMSynthesisError(
            "No synthesis model configured",
            provider=canonical,
        )

    if canonical == "anthropic":
        return await _call_anthropic_messages(
            system=system,
            user=user,
            model=selected_model,
            max_tokens=max_tokens,
        )
    if canonical == "gemini":
        return await _call_gemini_generate_content(
            system=system,
            user=user,
            model=selected_model,
            max_tokens=max_tokens,
            response_schema=response_schema,
        )
    return await _call_openai_compatible_chat(
        system=system,
        user=user,
        provider=canonical,
        model=selected_model,
        max_tokens=max_tokens,
        response_schema=response_schema,
    )


async def _call_openai_compatible_chat(
    *,
    system: str,
    user: str,
    provider: str,
    model: str,
    max_tokens: int,
    response_schema: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if provider == "deepseek":
        api_key = settings.deepseek_api_key
        url = _deepseek_chat_url(settings.deepseek_api_base_url)
    elif provider == "openai":
        api_key = settings.openai_api_key
        url = "https://api.openai.com/v1/chat/completions"
    else:
        raise LLMSynthesisError(
            f"Unsupported OpenAI-compatible provider: {provider}",
            provider=provider,
        )
    if not api_key:
        raise MissingLLMKeyError(
            f"{provider} API key is not configured",
            provider=provider,
        )

    # OpenAI enforces a JSON Schema natively (json_schema response format);
    # DeepSeek supports only json_object, so there the schema is advisory.
    if response_schema and provider == "openai":
        response_format: Dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "schema": dict(response_schema),
                "strict": True,
            },
        }
    else:
        response_format = {"type": "json_object"}
    body: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": response_format,
        "temperature": 0.2,
        "max_tokens": int(max_tokens),
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = await _post_json(
        url=url,
        body=body,
        headers=headers,
        provider=provider,
    )
    choices = payload.get("choices") or []
    first_choice = choices[0] if choices and isinstance(choices[0], Mapping) else {}
    message = first_choice.get("message") if isinstance(first_choice, Mapping) else {}
    text = str((message or {}).get("content") or "")
    usage = _openai_usage(payload.get("usage"))
    return {
        "text": text,
        "usage": usage,
        "finish_reason": first_choice.get("finish_reason") if isinstance(first_choice, Mapping) else None,
        "provider": provider,
        "model": model,
    }


async def _call_anthropic_messages(
    *,
    system: str,
    user: str,
    model: str,
    max_tokens: int,
) -> Dict[str, Any]:
    api_key = settings.anthropic_api_key
    if not api_key:
        raise MissingLLMKeyError(
            "anthropic API key is not configured",
            provider="anthropic",
        )
    body: Dict[str, Any] = {
        "model": model,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": int(max_tokens),
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = await _post_json(
        url="https://api.anthropic.com/v1/messages",
        body=body,
        headers=headers,
        provider="anthropic",
    )
    text = _anthropic_text(payload)
    usage_block = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    return {
        "text": text,
        "usage": {
            "input_tokens": usage_block.get("input_tokens"),
            "output_tokens": usage_block.get("output_tokens"),
        },
        "provider": "anthropic",
        "model": model,
    }


async def _call_gemini_generate_content(
    *,
    system: str,
    user: str,
    model: str,
    max_tokens: int,
    response_schema: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    api_key = settings.gemini_api_key
    if not vertex_gemini.credentials_available(api_key):
        raise MissingLLMKeyError(
            "gemini credentials are not configured",
            provider="gemini",
        )
    generation_config: Dict[str, Any] = {
        "temperature": 0.2,
        "maxOutputTokens": int(max_tokens),
        # JSON mime type supports top-level arrays (unlike OpenAI's
        # json_object mode) — callers like extract_winnable_prompts ask
        # for a bare JSON array.
        "responseMimeType": "application/json",
        # Gemini 2.5 models spend maxOutputTokens on internal "thinking"
        # by default; with small caps (e.g. 400 for winnable prompts) the
        # thoughts consume the whole budget and the actual answer arrives
        # truncated to a fragment (prod run 370dde30: raw len 9 and 31 →
        # EMPTY prompts, audit degraded to branded-only). Disable thinking:
        # these are cheap extraction tasks, and this is also the fast/cheap
        # behavior the flash tier is chosen for.
        "thinkingConfig": {"thinkingBudget": 0},
    }
    # W3b: enforce the response SHAPE at the API when a schema is supplied.
    if response_schema:
        generation_config["responseSchema"] = dict(response_schema)
    body: Dict[str, Any] = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": generation_config,
    }
    headers = await vertex_gemini.auth_headers(api_key)
    payload = await _post_json(
        url=vertex_gemini.generate_content_url(model),
        body=body,
        headers=headers,
        provider="gemini",
    )
    text = _gemini_text(payload)
    usage_block = (
        payload.get("usageMetadata")
        if isinstance(payload.get("usageMetadata"), Mapping)
        else {}
    )
    candidates = payload.get("candidates") or []
    first = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
    return {
        "text": text,
        "usage": {
            "input_tokens": usage_block.get("promptTokenCount"),
            "output_tokens": usage_block.get("candidatesTokenCount"),
        },
        "finish_reason": first.get("finishReason") if isinstance(first, Mapping) else None,
        "provider": "gemini",
        "model": model,
    }


def _gemini_text(payload: Mapping[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0]
    if not isinstance(first, Mapping):
        return ""
    content = first.get("content")
    if not isinstance(content, Mapping):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, Mapping)
    )


async def _post_json(
    *,
    url: str,
    body: Dict[str, Any],
    headers: Dict[str, str],
    provider: str,
) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_S) as client:
            response = await client.post(url, json=body, headers=headers)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise LLMSynthesisHTTPError(
            f"{provider} synthesis transport failure: {exc}",
            provider=provider,
        ) from exc
    if response.status_code >= 400:
        raise LLMSynthesisHTTPError(
            f"{provider} synthesis HTTP {response.status_code}: {response.text[:200]}",
            provider=provider,
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMSynthesisHTTPError(
            f"{provider} synthesis returned invalid JSON",
            provider=provider,
        ) from exc
    if not isinstance(payload, dict):
        raise LLMSynthesisHTTPError(
            f"{provider} synthesis returned non-object JSON",
            provider=provider,
        )
    return payload


def _deepseek_chat_url(base_url: str) -> str:
    base = str(base_url or "https://api.deepseek.com").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _openai_usage(raw: Any) -> Dict[str, Any]:
    usage = raw if isinstance(raw, Mapping) else {}
    return {
        "input_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
        "output_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
    }


def _anthropic_text(payload: Mapping[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping)
        and (block.get("type") in (None, "text"))
        and str(block.get("text") or "")
    ]
    return "".join(parts)
