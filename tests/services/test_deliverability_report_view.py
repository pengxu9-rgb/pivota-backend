from __future__ import annotations


def _deliverability_report():
    return {
        "brand_rollup": {
            "deliverability": {
                "status_counts": {
                    "transactable": 1,
                    "servable_not_transactable": 1,
                    "not_publishable": 1,
                }
            }
        },
        "per_sku_reports": [
            {
                "sku_key": "sku-ready",
                "sku_title": "Ready Serum",
                "checkout_handoff": {
                    "status": "eligible",
                    "label": "Open buyable Pivota product page",
                    "handoff_url": "https://agent.pivota.cc/checkout/handoff?token=t",
                },
                "deliverability": {
                    "status": "transactable",
                    "summary": "This SKU is serving eligible and has a ready merchant-checkout path.",
                    "serving": {"status": "ready"},
                    "checkout": {"status": "ready", "commerce_path": "pivota_direct_quote_first"},
                },
            },
            {
                "sku_key": "sku-stock",
                "sku_title": "Unknown Stock Serum",
                "deliverability": {
                    "status": "servable_not_transactable",
                    "summary": "This SKU can be served, but checkout is not ready enough to promise a transaction.",
                    "serving": {"status": "ready"},
                    "checkout": {"status": "blocked", "reason": "explicit availability is missing"},
                },
            },
            {
                "sku_key": "sku-gated",
                "sku_title": "Gated Serum",
                "deliverability": {
                    "status": "not_publishable",
                    "summary": "This SKU should not be promised to buyers yet because serving eligibility is not confirmed.",
                    "serving": {"status": "unknown"},
                    "checkout": {"status": "ready"},
                },
            },
        ],
    }


def test_build_deliverability_render_view_summarizes_counts_and_rows():
    from services.deliverability_report_view import build_deliverability_render_view

    view = build_deliverability_render_view(_deliverability_report())

    assert view["headline"] == "1 of 3 audited SKUs is confirmed transactable."
    assert "explicit available-stock signal" in view["definition"]
    assert view["counts"] == [
        {"status": "transactable", "label": "Transactable", "count": 1},
        {
            "status": "servable_not_transactable",
            "label": "Servable, checkout not ready",
            "count": 1,
        },
        {"status": "not_publishable", "label": "Not publishable", "count": 1},
    ]
    assert view["transactable_rows"][0]["sku_title"] == "Ready Serum"
    assert view["transactable_rows"][0]["handoff_url"] == "https://agent.pivota.cc/checkout/handoff?token=t"
    assert view["transactable_rows"][0]["handoff_label"] == "Open buyable Pivota product page"
    assert [row["sku_title"] for row in view["attention_rows"]] == [
        "Unknown Stock Serum",
        "Gated Serum",
    ]


def test_build_deliverability_render_view_degrades_to_empty_without_data():
    from services.deliverability_report_view import build_deliverability_render_view

    assert build_deliverability_render_view({"merchant_name": "No SKU data"}) == {}


def test_build_deliverability_render_view_supports_single_sku_payload():
    from services.deliverability_report_view import build_deliverability_render_view

    view = build_deliverability_render_view(_deliverability_report()["per_sku_reports"][1])

    assert view["headline"] == "No audited SKU is confirmed transactable yet."
    assert view["counts"] == [
        {
            "status": "servable_not_transactable",
            "label": "Servable, checkout not ready",
            "count": 1,
        }
    ]
