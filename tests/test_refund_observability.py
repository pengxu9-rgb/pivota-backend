from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)


def test_collect_refund_ids_prefers_unique_stripe_refs() -> None:
    from services.refund_observability import collect_refund_ids

    metadata = {
        "stripe_refund_status": {
            "refund_id": "re_latest",
        },
        "stripe_refund_statuses": {
            "re_latest": {"refund_id": "re_latest"},
            "re_history": {"refund_id": "re_history"},
        },
        "psp_refund_records": {
            "stripe:re_history": {
                "psp": "stripe",
                "refund_reference": "re_history",
            },
            "adyen:foo": {
                "psp": "adyen",
                "refund_reference": "psp_adyen_1",
            },
        },
        "last_refund": {
            "psp": "stripe",
            "refund_reference": "re_fallback",
        },
    }

    assert collect_refund_ids(metadata, provider="stripe") == [
        "re_latest",
        "re_history",
        "re_fallback",
    ]


@pytest.mark.asyncio
async def test_refresh_stripe_refund_observability_for_order_backfills_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.refund_api as refund_api

    order = {
        "order_id": "ORD_REFUND_OBS",
        "merchant_id": "merch_obs",
        "psp_used": "stripe",
        "currency": "USD",
        "metadata": {
            "last_refund": {
                "psp": "stripe",
                "refund_reference": "re_hist_1",
                "currency": "USD",
            }
        },
    }
    updated_values: Dict[str, Any] = {}

    async def fake_resolve_adapter(_order: Dict[str, Any]):
        return "stripe", "sk_test", {}

    class _FakeAdapter:
        async def get_refund_details(self, refund_id: str):
            assert refund_id == "re_hist_1"
            return True, {
                "id": refund_id,
                "status": "succeeded",
                "amount": 953,
                "currency": "usd",
                "destination_details": {
                    "type": "card",
                    "card": {
                        "reference": "123456789012",
                        "reference_status": "available",
                        "reference_type": "acquirer_reference_number",
                        "type": "refund",
                    }
                },
            }, None

    async def fake_update_order(order_id: str, values: Dict[str, Any]) -> bool:
        assert order_id == "ORD_REFUND_OBS"
        updated_values.update(values)
        return True

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_REFUND_OBS"
        return {**order, "metadata": updated_values["metadata"]}

    monkeypatch.setattr(refund_api, "_resolve_refund_adapter", fake_resolve_adapter)
    monkeypatch.setattr(refund_api, "get_psp_adapter", lambda *_args, **_kwargs: _FakeAdapter())
    monkeypatch.setattr(refund_api, "update_order", fake_update_order)
    monkeypatch.setattr(refund_api, "get_order", fake_get_order)

    result = await refund_api._refresh_stripe_refund_observability_for_order(order)

    assert result["updated"] is True
    assert result["refreshed_count"] == 1
    assert result["refund_ids"] == ["re_hist_1"]
    assert updated_values["metadata"]["stripe_refund_status"]["refund_id"] == "re_hist_1"
    assert updated_values["metadata"]["stripe_refund_status"]["status"] == "succeeded"
    assert updated_values["metadata"]["stripe_refund_status"]["reference"] == "123456789012"
    assert result["refund_tracking"]["latest"]["tracking_reference_kind"] == "ARN"
