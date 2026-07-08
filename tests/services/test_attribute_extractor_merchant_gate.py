"""Phase 2c — per-merchant scoping for the attribute extractor.

A NON-EMPTY allowlist runs the extractor ONLY for those merchants (the safe way
to pilot on Mojawa without touching prod electronics/thin traffic). An EMPTY
allowlist keeps Phase-2b behavior (the global flag governs).
"""
import json

import pytest

import services.agent_center_bd_report_service as R
from config.settings import settings

MOJAWA = {
    "product_key": "prod::mojawa",
    "merchant_id": "m_mojawa",
    "title": "Mojawa Purra Swim",
    "product_type": "Bone Conduction Headphones",
    "attributes_raw": {"description": "open-ear bone conduction with IP68 for swimming"},
}
_LLM_OUT = {"attributes": [
    {"class_name": "certification_constraint", "value": "IP68", "span": "with IP68 for"},
]}


@pytest.fixture(autouse=True)
def _flag_on_no_persist(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "x"))

    async def _noop_persist(*a, **k):
        return None

    monkeypatch.setattr(R, "_persist_llm_attributes", _noop_persist)


def _synth(counter):
    async def fake(**kwargs):
        counter["n"] += 1
        return {"text": json.dumps(_LLM_OUT), "provider": kwargs["provider"], "model": kwargs["model"]}
    return fake


@pytest.mark.asyncio
async def test_empty_allowlist_runs_for_any_merchant(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_merchants_raw", "")
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _synth(counter))
    ctx = {"product": MOJAWA, "merchant_id": "m_mojawa", "vertical": "electronics"}
    await R._maybe_stash_llm_attributes(ctx)
    assert counter["n"] == 1


@pytest.mark.asyncio
async def test_allowlist_includes_merchant_runs(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_merchants_raw", "m_other, m_mojawa")
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _synth(counter))
    ctx = {"product": MOJAWA, "merchant_id": "m_mojawa", "vertical": "electronics"}
    await R._maybe_stash_llm_attributes(ctx)
    assert counter["n"] == 1
    assert R._LLM_ATTR_STASH_KEY in ctx


@pytest.mark.asyncio
async def test_allowlist_excludes_merchant_skips(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_merchants_raw", "m_other")
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _synth(counter))
    ctx = {"product": MOJAWA, "merchant_id": "m_mojawa", "vertical": "electronics"}
    await R._maybe_stash_llm_attributes(ctx)
    assert counter["n"] == 0                       # not in allowlist -> no LLM
    assert R._LLM_ATTR_STASH_KEY not in ctx


@pytest.mark.asyncio
async def test_merchant_id_read_from_product_when_absent_on_ctx(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_merchants_raw", "m_mojawa")
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _synth(counter))
    # ctx has no merchant_id key; it must fall back to product.merchant_id
    ctx = {"product": MOJAWA, "vertical": "electronics"}
    await R._maybe_stash_llm_attributes(ctx)
    assert counter["n"] == 1
