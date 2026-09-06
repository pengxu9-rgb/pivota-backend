"""The guardrails are actually WIRED — at staging and again at apply — on every
merchant-facing write lane that reaches merchant-visible state.

Each refusal here has a should-apply twin, because a guard that refuses everything is
as broken as one that refuses nothing, and because two of these lanes are live in
production today.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.database import database
from routes.employee_pdp_governance import router as employee_pdp_router
from services.merchant_write_guardrails import ACTOR_HUMAN, ACTOR_MODEL, ACTOR_SYSTEM
from utils.auth import get_current_employee

_ENRICH_MOD = "services.executor_agents.canonical_pdp_enrichment"


# ---------------------------------------------------------------------------
# Lane 1 — PDP governance module -> merchant_product_overlay
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not database.is_connected:
            await database.connect()
        try:
            yield
        finally:
            if database.is_connected:
                await database.disconnect()

    app = FastAPI(lifespan=lifespan)
    app.include_router(employee_pdp_router)
    app.dependency_overrides[get_current_employee] = lambda: {
        "sub": "employee-guardrail-test",
        "email": "employee@example.com",
        "role": "admin",
    }
    return TestClient(app)


def _resolve(client: TestClient, suffix: str) -> str:
    resolved = client.get(
        "/employee/pdps/resolve",
        params={"product_key": f"external_seed|external|{suffix}", "market": "US"},
    )
    assert resolved.status_code == 200, resolved.text
    return resolved.json()["pdp"]["pdp_id"]


def _draft(client: TestClient, pdp_id: str, payload: Dict[str, Any]):
    return client.post(
        f"/employee/pdps/{pdp_id}/modules/copy/draft",
        json={
            "payload": payload,
            "source_refs": [{"type": "external_seed", "id": "guardrail-test"}],
            "generated_by": "llm_candidate",
            "generation_ref": "gen-guardrail-test",
        },
    )


_CLEAN_COPY = {
    "title": "Plain Cotton Tee",
    "description": "Soft everyday shirt for casual wear.",
}


def test_staging_a_module_draft_refuses_a_price_riding_inside_a_copy_edit():
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-stage-price")

        refused = _draft(
            client,
            pdp_id,
            {**_CLEAN_COPY, "price": 4.99},
        )
        assert refused.status_code == 422, refused.text
        detail = refused.json()["detail"]
        assert detail["code"] == "MERCHANT_WRITE_GUARDRAIL"
        assert any(
            "cannot be changed through a content update" in v for v in detail["violations"]
        ), detail


def test_staging_a_module_draft_refuses_a_protected_identity_field():
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-stage-protected")
        refused = _draft(client, pdp_id, {**_CLEAN_COPY, "merchant_id": "someone-else"})
        assert refused.status_code == 422, refused.text
        assert any(
            "is protected" in v for v in refused.json()["detail"]["violations"]
        ), refused.text


def test_a_clean_module_draft_still_stages(monkeypatch):
    """The should-apply twin for both refusals above."""
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-stage-clean")
        ok = _draft(client, pdp_id, dict(_CLEAN_COPY))
        assert ok.status_code == 200, ok.text
        assert ok.json()["module"]["id"]


def test_staging_is_refused_by_the_items_cap_in_force_now(monkeypatch):
    monkeypatch.setenv("MERCHANT_WRITE_MAX_ITEMS", "1")
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-stage-items")
        refused = _draft(client, pdp_id, dict(_CLEAN_COPY))  # two fields, cap of one
        assert refused.status_code == 422, refused.text
        assert any(
            "touches 2 fields" in v for v in refused.json()["detail"]["violations"]
        ), refused.text


def test_publish_re_checks_the_guardrails_against_the_config_in_force_at_apply_time(
    monkeypatch,
):
    """A draft that was compliant when staged is refused at apply once an operator
    tightens the limit — the blueprint's rule that apply uses the config in force THEN,
    not the one that was in force at staging."""
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-apply-tightened")
        draft = _draft(client, pdp_id, dict(_CLEAN_COPY))
        assert draft.status_code == 200, draft.text
        draft_id = draft.json()["module"]["id"]

        # The tightening happens AFTER staging succeeded.
        monkeypatch.setenv("MERCHANT_WRITE_MAX_ITEMS", "1")
        published = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/publish",
            json={"version_id": draft_id},
        )
        assert published.status_code == 422, published.text
        assert any(
            "touches 2 fields" in v for v in published.json()["detail"]["violations"]
        ), published.text


def test_publish_succeeds_under_the_default_config(monkeypatch):
    """The should-apply twin: with nothing tightened, the same draft publishes. This is
    also the regression guard on production behaviour — the guardrails must not have
    made the ordinary human publish path fail closed."""
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-apply-default")
        draft = _draft(client, pdp_id, dict(_CLEAN_COPY))
        assert draft.status_code == 200, draft.text
        published = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/publish",
            json={"version_id": draft.json()["module"]["id"]},
        )
        assert published.status_code == 200, published.text
        assert published.json()["published"] is True


def test_the_machine_publish_lane_still_publishes_on_a_model_rubric_by_default():
    """Production behaviour, pinned. `require_host_approval_pdp_module_publish`
    defaults FALSE precisely so this lane — the designed machine-publish lane — keeps
    working. If a future change flips the default, this test says so out loud."""
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-machine-publish-default")
        draft = _draft(client, pdp_id, dict(_CLEAN_COPY))
        assert draft.status_code == 200, draft.text
        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/gpt55-review",
            json={
                "version_id": draft.json()["module"]["id"],
                "rubric": _passing_rubric(),
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["published"] is True


def test_flipping_require_host_approval_stops_the_model_from_publishing(monkeypatch):
    """The switch that makes model output unable to make the write land. The rubric
    below says "pass" with every check true — model output — and it sets nothing."""
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-machine-publish-gated")
        draft = _draft(client, pdp_id, dict(_CLEAN_COPY))
        assert draft.status_code == 200, draft.text
        draft_id = draft.json()["module"]["id"]

        monkeypatch.setenv("MERCHANT_WRITE_REQUIRE_HOST_APPROVAL_PDP_PUBLISH", "true")
        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/gpt55-review",
            json={"version_id": draft_id, "rubric": _passing_rubric()},
        )
        assert reviewed.status_code == 422, reviewed.text
        assert any(
            "a model verdict does not approve a merchant write" in v
            for v in reviewed.json()["detail"]["violations"]
        ), reviewed.text


def test_a_human_publish_is_unaffected_by_the_host_approval_switch(monkeypatch):
    """The twin: with the same switch ON, the human employee publish path still works —
    the switch bounds the MODEL, not the operator."""
    monkeypatch.setenv("MERCHANT_WRITE_REQUIRE_HOST_APPROVAL_PDP_PUBLISH", "true")
    with _client() as client:
        pdp_id = _resolve(client, "guardrail-human-publish-gated")
        draft = _draft(client, pdp_id, dict(_CLEAN_COPY))
        assert draft.status_code == 200, draft.text
        published = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/publish",
            json={"version_id": draft.json()["module"]["id"]},
        )
        assert published.status_code == 200, published.text
        assert published.json()["published"] is True


def _passing_rubric() -> Dict[str, Any]:
    return {
        "decision": "pass",
        "confidence": 0.94,
        "reasons": ["source-grounded low-risk copy reviewed in Codex window"],
        "checks": {
            "source_grounded": True,
            "seller_entity_checkout_not_confused": True,
            "variant_market_consistent": True,
            "no_medical_regulated_promo_or_fake_review_claim": True,
            "machine_publish_allowed_module": True,
        },
        "evidence_refs": ["external_seed:guardrail-test"],
        "reviewed_in": "codex_external_window",
    }


def test_the_actor_kind_of_a_model_review_is_model_however_the_actor_id_reads():
    """The merchant self-approve route passes actor_id="merchant:<id>" with actor_type
    REVIEW_ACTOR_GPT55, because the LLM gate is the publish authority there. Reading the
    id instead of the type would let a model-decided publish present itself as human."""
    from services.pdp_governance_service import (
        REVIEW_ACTOR_GPT55,
        REVIEW_ACTOR_HUMAN,
        REVIEW_ACTOR_SYSTEM,
        _guardrail_actor_kind,
    )

    assert _guardrail_actor_kind(REVIEW_ACTOR_GPT55) == ACTOR_MODEL
    assert _guardrail_actor_kind(REVIEW_ACTOR_HUMAN) == ACTOR_HUMAN
    assert _guardrail_actor_kind(REVIEW_ACTOR_SYSTEM) == ACTOR_SYSTEM
    # An unknown actor is a MODEL, not a human: unknown must never buy approval.
    assert _guardrail_actor_kind("something_new") == ACTOR_MODEL
    assert _guardrail_actor_kind(None) == ACTOR_MODEL


# ---------------------------------------------------------------------------
# Lane 2, apply — copy -> the merchant's LIVE Shopify store
# ---------------------------------------------------------------------------


def _store(**kw) -> Dict[str, Any]:
    base = {
        "platform": "shopify",
        "domain": "example.myshopify.com",
        "api_key_raw": "k",
        "store_id": "s1",
        "status": "active",
        "content_writeback_status": "enabled",
        "content_writeback_canary_product_id": None,
    }
    base.update(kw)
    return base


_OK_GRAPHQL = {
    "metafieldsSet": {
        "metafields": [{"id": "gid://1", "namespace": "pivota", "key": "ai_pdp"}],
        "userErrors": [],
    }
}


async def _publish(enrichment: Dict[str, Any], actor_kind: str):
    from services.shopify_content_writeback import publish_content_to_store

    gql = AsyncMock(return_value=_OK_GRAPHQL)
    with patch(
        "services.merchant_store_service.get_merchant_active_stores",
        new=AsyncMock(return_value=[_store()]),
    ), patch(
        "services.shopify_access_token_service.resolve_shopify_admin_access_token",
        new=AsyncMock(return_value=("tok", {})),
    ), patch(
        "services.shopify_graphql_client.shopify_admin_graphql", new=gql
    ):
        res = await publish_content_to_store(
            merchant_id="m1",
            platform="shopify",
            platform_product_id="p1",
            enrichment=enrichment,
            actor_kind=actor_kind,
        )
    return res, gql


_GOOD_ENRICHMENT = {
    "title_override": "Good Night Collagen",
    "summary_short": "Low-molecular collagen, 30 sticks.",
    "description_markdown": "Single-serve collagen sticks for a daily routine.",
    "bullet_points": ["30 sticks per box"],
    "usage_scenarios": ["Daily morning routine"],
}


@pytest.mark.asyncio
async def test_a_model_actor_can_never_publish_to_the_merchants_live_store():
    res, gql = await _publish(dict(_GOOD_ENRICHMENT), ACTOR_MODEL)
    assert res["status"] == "blocked"
    assert res["blocker"] == "host_approval_required"
    gql.assert_not_awaited()  # never reaches the store


@pytest.mark.asyncio
async def test_a_human_actor_publishes_the_same_copy():
    """The should-apply twin, and the production path: the merchant asked for it."""
    res, gql = await _publish(dict(_GOOD_ENRICHMENT), ACTOR_HUMAN)
    assert res["status"] == "written", res
    gql.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_store_write_is_refused_when_the_copy_breaks_a_size_ceiling(monkeypatch):
    monkeypatch.setenv("MERCHANT_WRITE_MAX_FIELD_CHARS", "50")
    res, gql = await _publish(
        {**_GOOD_ENRICHMENT, "description_markdown": "x" * 500}, ACTOR_HUMAN
    )
    assert res["status"] == "blocked"
    assert res["blocker"] == "write_guardrail"
    assert any("characters and the limit is 50" in v for v in res["violations"]), res
    gql.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_store_write_check_reads_the_blob_that_is_actually_sent(monkeypatch):
    """The guardrail runs on the built metafield value, not on the upstream enrichment:
    a long field the writeback does NOT send must not block a write."""
    monkeypatch.setenv("MERCHANT_WRITE_MAX_FIELD_CHARS", "200")
    res, gql = await _publish(
        {**_GOOD_ENRICHMENT, "audience_tags": ["y" * 5000]}, ACTOR_HUMAN
    )
    assert res["status"] == "written", res
    gql.assert_awaited_once()


# ---------------------------------------------------------------------------
# Lane 2, stage — the LLM's copy -> the product-enrichment overlay
# ---------------------------------------------------------------------------


def _candidate() -> Dict[str, Any]:
    return {
        "merchant_id": "m1",
        "platform": "shopify",
        "source_product_id": "sp-1",
        "content_key": "ck-1",
        "title": "Good Night Collagen, 30 sticks",
        "description": "thin",
        "brand": "BB Lab",
        "product_type": "supplement",
        "category": "health",
    }


def _generated(**overrides) -> Dict[str, Any]:
    base = {
        "description_markdown": (
            "Low-molecular-weight collagen in single-serve sticks. Each box has 30 "
            "sticks designed for daily use."
        ),
        "summary_short": "Low-molecular collagen, 30 single-serve sticks.",
        "bullet_points": ["30 sticks per box"],
        "usage_scenarios": ["Daily morning routine"],
        "audience_tags": ["adults"],
        "title_override": "Good Night Collagen (Low-Molecular), 30 Sticks",
    }
    base.update(overrides)
    return base


async def _run_enrichment(generated: Dict[str, Any]):
    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.canonical_pdp_enrichment import (
        CanonicalPdpEnrichmentAgent,
    )

    upsert = AsyncMock()
    verdict = {
        "passed": True,
        "reason": "grounded",
        "misstates_facts": False,
        "supports_recommendation": True,
        "note": "",
    }
    with patch(f"{_ENRICH_MOD}._resolve_gemini_api_key", return_value="k"), patch(
        f"{_ENRICH_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=verdict)
    ), patch(
        f"{_ENRICH_MOD}._fetch_thin_canonical_pdps",
        new=AsyncMock(return_value=[_candidate()]),
    ), patch(
        f"{_ENRICH_MOD}._generate_enrichment", new=AsyncMock(return_value=generated)
    ), patch(
        "db.product_enrichment.upsert_enrichment", new=upsert
    ), patch(
        "services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key",
        new=AsyncMock(return_value=True),
    ):
        result = await CanonicalPdpEnrichmentAgent().execute(ExecutorContext(merchant_id="m1"))
    return result, upsert


@pytest.mark.asyncio
async def test_the_enrichment_agent_stages_compliant_copy():
    """The should-apply twin, first: the lane still works."""
    result, upsert = await _run_enrichment(_generated())
    assert result.evidence["enriched_count"] == 1, result.evidence
    upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_enrichment_agent_refuses_copy_that_breaks_the_guardrails(monkeypatch):
    monkeypatch.setenv("MERCHANT_WRITE_MAX_FIELD_CHARS", "80")
    result, upsert = await _run_enrichment(
        _generated(description_markdown="x" * 4000)
    )
    assert result.evidence["enriched_count"] == 0, result.evidence
    assert result.evidence["blocked_count"] == 1, result.evidence
    blocked = result.evidence["blocked"][0]
    assert blocked["reason"] == "write_guardrail"
    assert blocked["gate"] == "merchant_write_guardrails"
    # Never persisted: the overlay this feeds is what the served PDP renders.
    upsert.assert_not_awaited()
