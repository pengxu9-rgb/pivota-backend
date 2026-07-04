"""ADR-006 Phase 3 — per-SKU request_indexing trigger + status read-back.

Service-level tests with the DB and the Pivota submit core mocked. Cover URL
resolution (sku -> signature -> canonical URL), the quota/idempotency dedupe,
the flag-off degrade, and the status read-back mapping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services import pivota_indexing_request as mod
from services.gsc_integration import GscNotConfiguredError


# ---- resolve_canonical_pdp_url --------------------------------------------

@pytest.mark.asyncio
async def test_resolve_builds_url_from_signature():
    rows = [
        {"product_key": "pk_1"},                                                  # catalog_skus
        {"pivota_canonical_url": None, "pivota_signature_id": "sig_abc",          # catalog_products
         "content_key": "ck_1"},
    ]

    async def _fetch_one(query, params):
        return rows.pop(0)

    with patch("db.database.database.fetch_one", AsyncMock(side_effect=_fetch_one)):
        url = await mod.resolve_canonical_pdp_url("sku_1", "m1")
    assert url == "https://agent.pivota.cc/products/sig_abc"


@pytest.mark.asyncio
async def test_resolve_prefers_stored_canonical_url():
    """When catalog_products already has the canonical URL, use it directly."""
    rows = [
        {"product_key": "pk_1"},
        {"pivota_canonical_url": "https://agent.pivota.cc/products/sig_stored",
         "pivota_signature_id": "sig_other", "content_key": "ck_1"},
    ]

    async def _fetch_one(query, params):
        return rows.pop(0)

    with patch("db.database.database.fetch_one", AsyncMock(side_effect=_fetch_one)):
        url = await mod.resolve_canonical_pdp_url("sku_1", "m1")
    assert url == "https://agent.pivota.cc/products/sig_stored"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_index_pipeline_state_merchant_scoped():
    """The index_pipeline_state fallback MUST be merchant-scoped — content_key
    is cross-merchant, so an unscoped query could resolve another merchant's
    sig/URL. Assert every query (incl. the fallback) binds merchant_id."""
    rows = [
        {"product_key": "pk_1"},                                                  # catalog_skus
        {"pivota_canonical_url": None, "pivota_signature_id": None,               # catalog_products
         "content_key": "ck_1"},
        {"pivota_signature_id": "sig_from_ips"},                                  # index_pipeline_state
    ]
    calls = []

    async def _fetch_one(query, params):
        calls.append((query, params))
        return rows.pop(0)

    with patch("db.database.database.fetch_one", AsyncMock(side_effect=_fetch_one)):
        url = await mod.resolve_canonical_pdp_url("sku_1", "m1")

    assert url == "https://agent.pivota.cc/products/sig_from_ips"
    # 3 queries ran, and EVERY one is scoped to the caller's merchant_id.
    assert len(calls) == 3
    assert all(c[1].get("merchant_id") == "m1" for c in calls)
    # specifically the fallback query hits index_pipeline_state with merchant_id
    fallback_q, fallback_p = calls[2]
    assert "index_pipeline_state" in fallback_q
    assert fallback_p["merchant_id"] == "m1"
    assert fallback_p["content_key"] == "ck_1"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_sku():
    with patch("db.database.database.fetch_one", AsyncMock(return_value=None)):
        assert await mod.resolve_canonical_pdp_url("sku_x", "m1") is None


# ---- URL-tier (url_audit) resolves through the SAME uniform path ----------

@pytest.mark.asyncio
async def test_resolve_url_audit_product_key_uniform_path():
    """A URL-tier seed carries no variant sku_key, so its CTA stamps the seed's
    catalog product_key directly. The resolver treats a target with no
    catalog_skus row AS a product_key and loads catalog_products via the SAME
    query a connected-store SKU uses — no url_audit branch."""
    product_key = "prod::m1::url_audit::host~abc123"
    rows = [
        None,                                                        # catalog_skus miss
        {"pivota_canonical_url": "https://agent.pivota.cc/products/sig_seed",
         "pivota_signature_id": "sig_seed", "content_key": "ck_seed"},  # catalog_products
    ]
    calls = []

    async def _fetch_one(query, params):
        calls.append((query, params))
        return rows.pop(0)

    with patch("db.database.database.fetch_one", AsyncMock(side_effect=_fetch_one)):
        url = await mod.resolve_canonical_pdp_url(product_key, "m1")

    assert url == "https://agent.pivota.cc/products/sig_seed"
    # The catalog_products lookup used the target AS the product_key (no sku
    # indirection), merchant-scoped on the caller's merchant_id.
    _, product_params = calls[1]
    assert product_params["product_key"] == product_key
    assert product_params["merchant_id"] == "m1"


@pytest.mark.asyncio
async def test_resolve_url_audit_no_seed_returns_none():
    """No seed row (intake off / guard-skipped) -> honest no canonical URL."""
    rows = [None, None]  # catalog_skus miss, then catalog_products miss

    async def _fetch_one(query, params):
        return rows.pop(0)

    with patch("db.database.database.fetch_one", AsyncMock(side_effect=_fetch_one)):
        url = await mod.resolve_canonical_pdp_url(
            "prod::m1::url_audit::host~abc123", "m1"
        )
    assert url is None
    # request_sku_indexing preserves the honest status on top of it.
    with patch.object(mod, "resolve_canonical_pdp_url", AsyncMock(return_value=None)):
        res = await mod.request_sku_indexing("prod::m1::url_audit::host~abc123", "m1")
    assert res["status"] == "no_canonical_url"


@pytest.mark.asyncio
async def test_resolve_returns_none_on_empty_inputs():
    assert await mod.resolve_canonical_pdp_url("", "m1") is None
    assert await mod.resolve_canonical_pdp_url("sku_1", "") is None


# ---- request_sku_indexing -------------------------------------------------

@pytest.mark.asyncio
async def test_request_no_canonical_url():
    with patch.object(mod, "resolve_canonical_pdp_url", AsyncMock(return_value=None)):
        res = await mod.request_sku_indexing("sku_1", "m1")
    assert res["status"] == "no_canonical_url"


@pytest.mark.asyncio
async def test_request_submits_when_not_yet_submitted():
    url = "https://agent.pivota.cc/products/sig_abc"
    with patch.object(mod, "resolve_canonical_pdp_url", AsyncMock(return_value=url)), \
         patch.object(mod, "_get_submission_row", AsyncMock(return_value=None)), \
         patch("services.gsc_integration.submit_pivota_canonical_url",
               AsyncMock(return_value={"status": "submitted", "last_status": "submitted"})):
        res = await mod.request_sku_indexing("sku_1", "m1")
    assert res["status"] == "submitted"
    assert res["url"] == url


@pytest.mark.asyncio
async def test_request_dedupes_inflight_url():
    """Already submitted/indexed -> no re-submit (quota guard)."""
    url = "https://agent.pivota.cc/products/sig_abc"
    submit = AsyncMock()
    with patch.object(mod, "resolve_canonical_pdp_url", AsyncMock(return_value=url)), \
         patch.object(mod, "_get_submission_row",
                      AsyncMock(return_value={"last_status": "submitted"})), \
         patch("services.gsc_integration.submit_pivota_canonical_url", submit):
        res = await mod.request_sku_indexing("sku_1", "m1")
    assert res["status"] == "submitted"
    assert res["deduped"] is True
    submit.assert_not_awaited()  # never re-submitted


@pytest.mark.asyncio
async def test_request_degrades_when_flag_off():
    url = "https://agent.pivota.cc/products/sig_abc"
    with patch.object(mod, "resolve_canonical_pdp_url", AsyncMock(return_value=url)), \
         patch.object(mod, "_get_submission_row", AsyncMock(return_value=None)), \
         patch("services.gsc_integration.submit_pivota_canonical_url",
               AsyncMock(side_effect=GscNotConfiguredError("flag off"))):
        res = await mod.request_sku_indexing("sku_1", "m1")
    assert res["status"] == "not_enabled"


# ---- get_sku_indexing_status ----------------------------------------------

@pytest.mark.asyncio
async def test_status_not_submitted_when_no_row():
    url = "https://agent.pivota.cc/products/sig_abc"
    with patch.object(mod, "resolve_canonical_pdp_url", AsyncMock(return_value=url)), \
         patch.object(mod, "_get_submission_row", AsyncMock(return_value=None)):
        res = await mod.get_sku_indexing_status("sku_1", "m1")
    assert res["status"] == "not_submitted"


@pytest.mark.asyncio
async def test_status_maps_indexed_row():
    url = "https://agent.pivota.cc/products/sig_abc"
    row = {"last_status": "indexed", "submitted_at": "2026-06-17",
           "indexed_at": "2026-06-20", "last_status_at": "2026-06-20"}
    with patch.object(mod, "resolve_canonical_pdp_url", AsyncMock(return_value=url)), \
         patch.object(mod, "_get_submission_row", AsyncMock(return_value=row)):
        res = await mod.get_sku_indexing_status("sku_1", "m1")
    assert res["status"] == "indexed"
    assert res["indexed_at"] == "2026-06-20"
    assert res["url"] == url


@pytest.mark.asyncio
async def test_status_no_canonical_url():
    with patch.object(mod, "resolve_canonical_pdp_url", AsyncMock(return_value=None)):
        res = await mod.get_sku_indexing_status("sku_1", "m1")
    assert res["status"] == "no_canonical_url"
