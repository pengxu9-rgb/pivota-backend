"""Phase 2b-2 — durable extractor cache: source-hash read-through + write.

A re-audit of an unchanged SKU must NOT re-pay the LLM; a product COPY change must
invalidate the cache (a stale span can't seed a probe for a spec the edited page
no longer states). Negative results (no attributes) are cached too.
"""
import json

import pytest

import services.agent_center_bd_report_service as R
from config.settings import settings
from services import llm_attribute_extractor as ax

MOJAWA = {
    "product_key": "prod::m1::shopify::mojawa-purra",
    "merchant_id": "m1",
    "title": "Mojawa Purra Swim",
    "product_type": "Bone Conduction Headphones",
    "attributes_raw": {
        "description": (
            "Purra Swim is an open-ear bone conduction headphone with IP68 "
            "waterproof rating, built for swimming with 32GB of onboard MP3 storage."
        ),
    },
}
_LLM_OUT = {
    "attributes": [
        {"class_name": "certification_constraint", "value": "IP68", "span": "IP68 waterproof rating"},
        {"class_name": "use_case", "value": "swimming", "span": "built for swimming with"},
    ]
}


def _fp(product):
    return ax.source_fingerprint(ax.build_source_text(product))


def _synth_factory(counter):
    async def fake(**kwargs):
        counter["n"] += 1
        return {"text": json.dumps(_LLM_OUT), "provider": kwargs["provider"], "model": kwargs["model"]}
    return fake


# ------------------------------ pure helpers ------------------------------ #

def test_source_fingerprint_stable_and_copy_sensitive():
    a = ax.source_fingerprint("IP68 waterproof, for swimming")
    assert a == ax.source_fingerprint("IP68  waterproof,  for swimming")   # ws/punct-insensitive
    assert a != ax.source_fingerprint("IP67 sweatproof only")               # real change busts it


def test_serialize_deserialize_round_trip():
    grounded = ax.ground_extracted_attributes(_LLM_OUT["attributes"], ax.build_source_text(MOJAWA))
    restored = ax.deserialize_grounded(ax.serialize_grounded(grounded))
    assert [(g.class_name, g.value, g.span) for g in restored] == [
        (g.class_name, g.value, g.span) for g in grounded
    ]


def test_cached_helper_hit_and_miss():
    fp = _fp(MOJAWA)
    hit_product = {**MOJAWA, "llm_attributes": {"source_hash": fp, "attributes": _LLM_OUT["attributes"]}}
    assert R._cached_llm_attributes(hit_product, fp) is not None
    # stale hash -> miss
    stale = {**MOJAWA, "llm_attributes": {"source_hash": "deadbeef", "attributes": _LLM_OUT["attributes"]}}
    assert R._cached_llm_attributes(stale, fp) is None
    # no cache column -> miss
    assert R._cached_llm_attributes(MOJAWA, fp) is None


# ------------------------------ read-through ------------------------------ #

@pytest.mark.asyncio
async def test_cache_hit_skips_llm(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "x"))
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _synth_factory(counter))
    fp = _fp(MOJAWA)
    product = {**MOJAWA, "llm_attributes": {"source_hash": fp, "attributes": _LLM_OUT["attributes"]}}
    ctx = {"product": product, "vertical": "electronics"}

    await R._maybe_stash_llm_attributes(ctx)

    assert counter["n"] == 0                                  # cache hit -> no LLM
    assert {g.value for g in ctx[R._LLM_ATTR_STASH_KEY]} == {"IP68", "swimming"}


@pytest.mark.asyncio
async def test_cache_stale_reextracts(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "x"))
    counter = {"n": 0}
    monkeypatch.setattr("services.llm_synthesis.synthesize", _synth_factory(counter))
    monkeypatch.setattr(R, "_persist_llm_attributes", _noop_persist())
    product = {**MOJAWA, "llm_attributes": {"source_hash": "stale", "attributes": []}}
    ctx = {"product": product, "vertical": "electronics"}

    await R._maybe_stash_llm_attributes(ctx)

    assert counter["n"] == 1                                  # stale -> re-extract
    assert ctx[R._LLM_ATTR_STASH_KEY]


def _noop_persist():
    async def _p(*a, **k):
        return None
    return _p


# -------------------------------- write path -------------------------------- #

@pytest.mark.asyncio
async def test_persist_writes_payload_for_catalog_sku(monkeypatch):
    calls = []

    async def fake_execute(query, values=None):
        calls.append((query, values))

    monkeypatch.setattr("db.database.database.execute", fake_execute)
    grounded = ax.ground_extracted_attributes(_LLM_OUT["attributes"], ax.build_source_text(MOJAWA))
    await R._persist_llm_attributes(MOJAWA, "fp123", grounded)

    assert len(calls) == 1
    query, values = calls[0]
    assert "UPDATE catalog_products" in query and "llm_attributes = CAST(:payload AS jsonb)" in query
    assert values["product_key"] == MOJAWA["product_key"] and values["merchant_id"] == "m1"
    payload = json.loads(values["payload"])
    assert payload["source_hash"] == "fp123"
    assert {a["value"] for a in payload["attributes"]} == {"IP68", "swimming"}


@pytest.mark.asyncio
async def test_persist_skips_url_audit_synthetic(monkeypatch):
    calls = []

    async def fake_execute(query, values=None):
        calls.append(1)

    monkeypatch.setattr("db.database.database.execute", fake_execute)
    synthetic = {k: v for k, v in MOJAWA.items() if k not in ("product_key", "merchant_id")}
    await R._persist_llm_attributes(synthetic, "fp", [])
    assert calls == []                                       # no catalog row -> no write


@pytest.mark.asyncio
async def test_extraction_persists_negative_result(monkeypatch):
    monkeypatch.setattr(settings, "attribute_extractor_enabled", True)
    monkeypatch.setattr(R, "_resolve_extractor_provider", lambda: ("deepseek", "x"))

    async def empty_synth(**kwargs):
        return {"text": json.dumps({"attributes": []}), "provider": kwargs["provider"], "model": kwargs["model"]}

    monkeypatch.setattr("services.llm_synthesis.synthesize", empty_synth)
    persisted = {}

    async def capture_persist(product, fp, grounded):
        persisted["fp"] = fp
        persisted["n"] = len(grounded)

    monkeypatch.setattr(R, "_persist_llm_attributes", capture_persist)
    ctx = {"product": MOJAWA, "vertical": "electronics"}

    await R._maybe_stash_llm_attributes(ctx)

    assert persisted["fp"] == _fp(MOJAWA)                    # negative result still cached
    assert persisted["n"] == 0
    assert R._LLM_ATTR_STASH_KEY not in ctx
