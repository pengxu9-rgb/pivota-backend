from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def signing_secret(monkeypatch):
    monkeypatch.setenv("MERCHANT_WEB_COLLECTOR_SIGNING_SECRET", "s" * 48)


def test_shopify_pixel_token_is_store_bound_and_separate_from_web_token():
    from services.merchant_web_collector_service import (
        WebCollectorError,
        issue_shopify_pixel_token,
        verify_shopify_pixel_token,
        verify_web_collector_token,
    )

    issued = issue_shopify_pixel_token(
        merchant_id="merch_1",
        store_id="store_shopify",
        now=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    claims = verify_shopify_pixel_token(issued["token"])
    assert claims["merchant_id"] == "merch_1"
    assert claims["store_id"] == "store_shopify"
    assert claims["platform"] == "shopify"
    with pytest.raises(WebCollectorError):
        verify_web_collector_token(
            issued["token"], request_origin="https://shop.example"
        )


def test_shopify_pixel_batch_keeps_public_collector_authority_boundary():
    from services.merchant_web_collector_service import build_web_collector_batch

    claims = {
        "merchant_id": "merch_1",
        "store_id": "store_shopify",
        "platform": "shopify",
    }
    batch = build_web_collector_batch(
        {
            "events": [
                {
                    "event_id": "shopify_pixel:evt_1",
                    "event_type": "checkout.submitted",
                    "occurred_at": "2026-08-31T12:00:00Z",
                    "session_id": "client_1",
                    "checkout_id": "checkout_1",
                }
            ]
        },
        claims=claims,
        source="shopify_web_pixel",
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )
    assert batch.events[0].source == "shopify_web_pixel"
    assert batch.events[0].platform == "shopify"


@pytest.mark.parametrize(
    "event_type", ["order.created", "payment.succeeded", "refund.succeeded"]
)
def test_shopify_pixel_cannot_report_authoritative_events(event_type):
    from services.merchant_web_collector_service import (
        WebCollectorError,
        build_web_collector_batch,
    )

    with pytest.raises(WebCollectorError):
        build_web_collector_batch(
            {
                "events": [
                    {
                        "event_id": "evt_bad",
                        "event_type": event_type,
                        "occurred_at": "2026-08-31T12:00:00Z",
                        "session_id": "client_1",
                    }
                ]
            },
            claims={"merchant_id": "m", "store_id": "s", "platform": "shopify"},
            source="shopify_web_pixel",
            now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        )
