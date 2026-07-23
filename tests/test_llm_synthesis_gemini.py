"""Gemini in the backend-direct synthesis layer (llm_synthesis)."""
from __future__ import annotations

import pytest

from config.settings import settings
from services import llm_synthesis as mod


def test_gemini_provider_resolution(monkeypatch):
    assert mod.normalize_provider("gemini") == "gemini"
    assert mod.normalize_provider("google") == "gemini"
    monkeypatch.setattr(settings, "gemini_synthesis_model", "gemini-2.5-flash", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "gk-test", raising=False)
    assert mod.default_model_for_provider("gemini") == "gemini-2.5-flash"
    assert mod.configured_key_for_provider("gemini") == "gk-test"


def test_gemini_text_extraction_shapes():
    payload = {
        "candidates": [{
            "content": {"parts": [{"text": '["a", '}, {"text": '"b"]'}]},
            "finishReason": "STOP",
        }],
    }
    assert mod._gemini_text(payload) == '["a", "b"]'
    assert mod._gemini_text({}) == ""
    assert mod._gemini_text({"candidates": [{}]}) == ""
    # Safety-blocked candidate (no content) degrades to empty, not a crash.
    assert mod._gemini_text({"candidates": [{"finishReason": "SAFETY"}]}) == ""


@pytest.mark.asyncio
async def test_gemini_synthesize_call_shape(monkeypatch):
    """synthesize(provider=gemini) posts system_instruction + JSON mime config
    to the generateContent endpoint with the key header, and returns the
    standard {text, usage, provider, model} shape."""
    monkeypatch.setattr(settings, "gemini_api_key", "gk-test", raising=False)
    captured = {}

    async def fake_post_json(*, url, body, headers, provider):
        captured.update({"url": url, "body": body, "headers": headers, "provider": provider})
        return {
            "candidates": [{
                "content": {"parts": [{"text": '["best k beauty hair oil for damaged hair"]'}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
        }

    monkeypatch.setattr(mod, "_post_json", fake_post_json)
    out = await mod.synthesize(
        system="sys", user="usr", provider="gemini",
        model="gemini-2.5-flash", max_tokens=400,
    )
    assert out["provider"] == "gemini"
    assert out["model"] == "gemini-2.5-flash"
    assert out["text"].startswith('["best k beauty')
    assert out["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert "models/gemini-2.5-flash:generateContent" in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "gk-test"
    assert captured["body"]["system_instruction"]["parts"][0]["text"] == "sys"
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert captured["body"]["generationConfig"]["maxOutputTokens"] == 400
    # Gemini 2.5 thinking must be OFF: with small output caps the default
    # thinking budget consumes maxOutputTokens and truncates the answer to a
    # fragment (prod run 370dde30 -> EMPTY winnable prompts).
    assert captured["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


@pytest.mark.asyncio
async def test_gemini_missing_key_raises_typed_error(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    with pytest.raises(mod.MissingLLMKeyError):
        await mod.synthesize(
            system="s", user="u", provider="gemini",
            model="gemini-2.5-flash", max_tokens=100,
        )


def test_provider_available_gemini_uses_vertex_when_no_key(monkeypatch):
    """Provider selection must treat gemini as available on Vertex even with no
    GEMINI_API_KEY — otherwise retiring the key silently reroutes brief/prompt-gen
    to deepseek. Off Vertex it stays gated on the configured key."""
    from services import vertex_gemini

    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)

    # Vertex ADC available, no raw key -> gemini is selectable.
    monkeypatch.setattr(vertex_gemini, "credentials_available", lambda *a, **k: True)
    assert mod.provider_available("gemini") is True

    # No credential of any kind -> not selectable (falls back).
    monkeypatch.setattr(vertex_gemini, "credentials_available", lambda *a, **k: False)
    assert mod.provider_available("gemini") is False

    # Other providers remain gated purely on their configured key.
    monkeypatch.setattr(mod, "configured_key_for_provider",
                        lambda p: "k" if p == "deepseek" else "", raising=False)
    assert mod.provider_available("deepseek") is True
    assert mod.provider_available("openai") is False
