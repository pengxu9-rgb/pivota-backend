from __future__ import annotations

from mvp.dispute_evidence import build_dispute_pack_manifest


def test_build_dispute_pack_manifest_contains_required_sections():
    manifest = build_dispute_pack_manifest(
        merchant_id="merch_1",
        dispute_ref="dp_1",
        order={
            "order_id": "ord_1",
            "shopify_order_id": "sh_1",
            "created_at": None,
            "paid_at": None,
            "currency": "USD",
            "total": "10.00",
            "items": [{"product_id": "p1", "quantity": 1}],
            "tracking_number": "trk_1",
            "fulfillment_status": "shipped",
        },
        dispute_payload={"status": "open", "reason": "fraud", "amount": "10.00", "currency": "USD"},
        status="draft",
        policy_rows=[{"policy_type": "refund", "url": "https://x", "hash_sha256": "h1", "updated_at": None}],
        audit_refs=[{"event_id": "evt_1", "action": "submit_payment.intent"}],
        triggered_by="unit_test",
    )
    assert manifest["pack_type"] == "dispute_pack"
    assert manifest["dispute"]["dispute_ref"] == "dp_1"
    assert "policy_snapshot" in manifest
    assert "audit_trail" in manifest
    assert "fulfillment_proof" in manifest
    assert manifest.get("manifest_sha256")
