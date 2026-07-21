"""Reconciliation must match the two product_key formats that coexist:
the canonical URL (`https://agent.pivota.cc/products/sig_<hex>`, used by legacy
tasks) and the catalog key (`prod::merch::shopify::ID`, used by per-SKU reports +
new tasks). They share the Pivota signature (`sig_<hex>`); match on it so a
per-SKU audit closes the audited product's legacy leftovers (page-usability Step 1
residual found by live eyeball).
"""

import pytest

from services import task_queue_service as tqs


def test_product_id_variants_extracts_signature():
    # catalog form
    assert "prod::m::shopify::123" in tqs._product_id_variants("prod::m::shopify::123")
    # canonical URL form -> raw + the bare sig
    v = tqs._product_id_variants("https://agent.pivota.cc/products/sig_586147c399a05451ccd799cf9e82eab7")
    assert "sig_586147c399a05451ccd799cf9e82eab7" in v
    assert "https://agent.pivota.cc/products/sig_586147c399a05451ccd799cf9e82eab7" in v


def test_covered_keys_collects_signature_from_identity_canonical_url():
    report = {
        "per_sku_reports": [{
            "product_key": "prod::m::shopify::10100856914217",
            "sku_key": "sku-1",
            "identity": {
                "canonical_url": "https://agent.pivota.cc/products/sig_586147c399a05451ccd799cf9e82eab7",
            },
        }],
    }
    covered = tqs._covered_product_keys(report)
    assert "prod::m::shopify::10100856914217" in covered
    assert "sig_586147c399a05451ccd799cf9e82eab7" in covered  # the shared id


@pytest.mark.asyncio
async def test_reconcile_closes_legacy_url_keyed_task_for_audited_product(monkeypatch):
    """A legacy task keyed by the canonical URL is closed when the audit covered
    that product (keyed by catalog id) — they match on the shared sig."""
    covered = tqs._covered_product_keys({
        "per_sku_reports": [{
            "product_key": "prod::m::shopify::10100856914217",
            "identity": {"canonical_url": "https://agent.pivota.cc/products/sig_abc12345"},
        }],
    })
    stale = [
        # legacy collagen task keyed by canonical URL (the audited product) -> close
        {"task_id": "legacy-index", "evidence": {
            "product_key": "https://agent.pivota.cc/products/sig_abc12345"}},
        # a different (un-audited) product -> keep
        {"task_id": "other-prod", "evidence": {
            "product_key": "https://agent.pivota.cc/products/sig_999deadbeef"}},
        # brand-level (no product_key) -> close
        {"task_id": "brand", "evidence": {}},
    ]
    closed = []

    async def _fake_list(**kwargs):
        return stale

    async def _fake_mark(*, task_id, superseded_by_task_id=None):
        closed.append(task_id)
        return True

    monkeypatch.setattr("db.merchant_tasks.list_pending_audit_tasks_excluding_run", _fake_list)
    monkeypatch.setattr("db.merchant_tasks.mark_task_superseded", _fake_mark)

    n = await tqs._reconcile_dropped_pending_tasks(
        merchant_id="m", audit_run_id="run-new", covered_product_keys=covered,
    )
    assert n == 2
    assert set(closed) == {"legacy-index", "brand"}
    assert "other-prod" not in closed  # un-audited product preserved (scope-aware)
