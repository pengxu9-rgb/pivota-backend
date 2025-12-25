from __future__ import annotations

from services.pcs_fact_ingest import build_internal_fact_dedupe_key, build_shopify_fact_dedupe_key


def test_build_shopify_fact_dedupe_key_is_stable():
    assert build_shopify_fact_dedupe_key(idempotency_key="shop:topic:wid") == "shopify:shop:topic:wid"


def test_build_internal_fact_dedupe_key_prefers_idempotency_key():
    k1 = build_internal_fact_dedupe_key(fact_type="internal.order_created", order_id="ord_1", idempotency_key="idem_1")
    k2 = build_internal_fact_dedupe_key(fact_type="internal.order_created", order_id="ord_1", idempotency_key="idem_1")
    assert k1 == k2
    assert k1.startswith("internal:internal.order_created:idem_1")


def test_build_internal_fact_dedupe_key_falls_back_to_order_id():
    k = build_internal_fact_dedupe_key(fact_type="internal.payment_updated", order_id="ord_2", idempotency_key=None)
    assert k == "internal:internal.payment_updated:ord_2"

