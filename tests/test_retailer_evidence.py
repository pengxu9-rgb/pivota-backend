"""Tier-1 retailer-evidence recycling into prompt generation.

Prior runs' third-party retailer excerpts (already captured as grounded
evidence) feed the attribute graph + LLM prompt extraction for thin own-page
fetches — first-party and gray-market hosts excluded, provenance-tagged,
vocabulary only (never claim substantiation)."""
from __future__ import annotations

import json

import pytest

from services.retailer_evidence import harvest_retailer_excerpts


def _vge(excerpt, titles):
    return {
        "probe_run_id": "r:1",
        "query": "q",
        "evidence_excerpt": excerpt,
        "grounding_sources": [
            {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/x", "title": t}
            for t in titles
        ],
    }


def _report(entries, sku_key="urlwedge:abc"):
    return {
        "merchant_name": "ANUKO",
        "merchant_domain": "anukoofficial.com",
        "per_sku_reports": [{
            "sku_key": sku_key,
            "verbatim_grounding_evidence": entries,
        }],
    }


LONG = "x" * 60  # padding so excerpts clear the 80-char floor


def test_harvest_keeps_retailer_drops_first_party_and_gray_market():
    entries = [
        _vge("Olive Young lists Anuko Bond & Repair Hair Oil with argan oil. " + LONG,
             ["oliveyoung.com"]),
        # own-domain only → excluded (own-page text flows in directly)
        _vge("Official site lists 20ml and 75ml sizes with shipping details. " + LONG,
             ["anukoofficial.com"]),
        # brand-affix second storefront only → excluded (first-party)
        _vge("The official Anuko website also features customer reviews. " + LONG,
             ["tryanuko.com"]),
        # gray-market/secondhand only → excluded
        _vge("Bunjang has secondhand listings of the hair oil at low prices. " + LONG,
             ["bunjang.co.kr"]),
        # mixed: one third-party host qualifies the entry
        _vge("Coupang lists 아누코 본드 앤 리페어 헤어 오일 극손상 에센스 아르간오일 75ml. " + LONG,
             ["tryanuko.com", "coupang.com"]),
    ]
    out = harvest_retailer_excerpts(
        _report(entries), sku_key="urlwedge:abc",
        merchant_domain="anukoofficial.com", merchant_brand="ANUKO",
    )
    joined = " ".join(out["excerpts"])
    assert "Olive Young lists" in joined
    assert "Coupang lists" in joined
    assert "Official site lists" not in joined
    assert "customer reviews" not in joined
    assert "Bunjang" not in joined
    assert set(out["hosts"]) == {"oliveyoung.com", "coupang.com"}


def test_harvest_respects_sku_key_and_caps():
    entries = [
        _vge(f"Retailer excerpt number {i}. " + "y" * 300, ["oliveyoung.com"])
        for i in range(10)
    ]
    out = harvest_retailer_excerpts(
        _report(entries, sku_key="urlwedge:abc"), sku_key="urlwedge:abc",
        merchant_domain="anukoofficial.com", merchant_brand="ANUKO",
    )
    assert 0 < len(out["excerpts"]) <= 6
    assert sum(len(x) for x in out["excerpts"]) <= 1500
    # different sku_key → nothing
    empty = harvest_retailer_excerpts(
        _report(entries, sku_key="urlwedge:abc"), sku_key="urlwedge:OTHER",
        merchant_domain="anukoofficial.com", merchant_brand="ANUKO",
    )
    assert empty == {"excerpts": [], "hosts": []}


def test_attribute_graph_ingests_retailer_excerpts_as_body_class():
    """Retailer excerpts can surface ingredients/certifications the thin
    own-page never states — but as a NON-authoritative source they must not
    set the format class."""
    from services.sku_sidewalk import build_sku_attribute_graph

    product = {
        "title": "아누코 본드 앤 리페어 헤어 오일 75ml",
        "brand": "ANUKO",
        # NOTE: uses lexicon terms (niacinamide/vegan) to prove the MECHANISM;
        # haircare ingredients (argan/shea/yuja) are a known lexicon-coverage
        # gap tracked with the scenario-ontology work.
        "_retailer_excerpts": [
            "Coupang lists the bond repair hair oil with niacinamide, vegan "
            "formula, in a powder-free lightweight texture for damaged hair.",
        ],
    }
    graph = build_sku_attribute_graph(product)
    classes = graph.get("classes") or {}
    assert "niacinamide" in (classes.get("ingredient") or [])
    assert "vegan" in (classes.get("certification_constraint") or [])
    # provenance: evidence maps the attribute back to the retailer source
    evidence = graph.get("evidence") or {}
    assert any("retailer_excerpt" in str(v) for v in evidence.values())
    # format is authoritative-only ("powder" in excerpt must not set it)
    assert "powder" not in (classes.get("format") or [])


@pytest.mark.asyncio
async def test_winnable_extractor_receives_retailer_excerpts(monkeypatch):
    from config.settings import settings as app_settings
    import services.llm_synthesis as llm
    from services import agent_center_bd_report_service as svc

    monkeypatch.setattr(app_settings, "prompt_gen_provider", "deepseek", raising=False)
    captured = {}

    async def fake_synthesize(**kwargs):
        captured.update(kwargs)
        return {"text": json.dumps(["argan oil bond repair treatment for damaged hair"])}

    monkeypatch.setattr(llm, "synthesize", fake_synthesize, raising=False)
    monkeypatch.setattr(llm, "configured_key_for_provider", lambda p: "k", raising=False)
    monkeypatch.setattr(llm, "default_model_for_provider", lambda p: "m", raising=False)

    out = await svc.extract_winnable_prompts({
        "product": {
            "title": "아누코 본드 앤 리페어 헤어 오일",
            "vendor": "ANUKO",
            "_retailer_excerpts": ["Olive Young lists it with argan oil and 200C heat protection."],
        },
    })
    assert out
    assert "retailer_listing_excerpts" in captured["user"]
    assert "argan oil and 200C heat protection" in captured["user"]


@pytest.mark.asyncio
async def test_load_prior_scans_succeeded_runs_and_scopes_run_kind(monkeypatch):
    """Regression: recent_runs_for_merchant rows carry `status` and NO `stage`
    key — the old stage=='completed' filter skipped every run, so Tier-1
    recycling silently never engaged. Also: a urlwedge:* SKU's scan must ask
    for merchant_url runs (the only kind that can carry it)."""
    import db.merchant_audit_runs as mar
    from services.retailer_evidence import load_prior_retailer_evidence

    report = _report(
        [_vge("Olive Young lists it with argan oil. " + LONG, ["oliveyoung.co.kr"])],
    )
    captured = {}

    async def fake_recent(**kwargs):
        captured.update(kwargs)
        return [{
            "run_id": "run-1",
            "status": "succeeded",
            "subject_type": "merchant_url",
            # NO "stage" key — the trend projection never included one.
        }]

    async def fake_fetch(*, run_id):
        return {"run_id": run_id, "report_jsonb": report}

    monkeypatch.setattr(mar, "recent_runs_for_merchant", fake_recent)
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)

    out = await load_prior_retailer_evidence(merchant_id="m1", sku_key="urlwedge:abc")
    assert out["excerpts"], "succeeded prior run must be harvestable"
    assert captured["subject_type"] == "merchant_url"
