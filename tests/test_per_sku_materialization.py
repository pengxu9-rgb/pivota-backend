"""Per-SKU audits must materialize tasks (page-usability Step 1 foundation).

Per-SKU audits return findings under `per_sku_reports`, not `per_product`, so the
legacy `_extract_action_items` walk found nothing and these audits materialized
ZERO tasks — the action plan never reflected the audits merchants actually run.
This bridges each SKU's `next_best_action` into one task.
"""

from services.task_queue_service import _extract_action_items, _per_sku_action_items


def _per_sku_report():
    return {
        "audit_mode": "per_sku",
        "per_sku_reports": [
            {
                "sku_key": "sku-collagen",
                "product_key": "https://agent.pivota.cc/products/sig_abc",
                "identity": {"name": "Good Night Collagen"},
                "scores": {"citation": {"score": 27}},
                "next_best_action": {
                    "headline": "Fill the gaps on Good Night Collagen's page before chasing reach",
                    "why_this_first": "AI can't recommend a product it can't read.",
                    "first_move": "Add the missing enrichment coverage.",
                    "tracking_metrics": ["enrichment coverage completeness", "citation rate"],
                    "cta": {"label": "Add on your Pivota page", "action": "request_enrichment"},
                },
            },
            {
                "sku_key": "sku-grape",
                "product_key": "https://agent.pivota.cc/products/sig_xyz",
                "identity": {"name": "Triple Shine Grape"},
                "scores": {"citation": {"score": 80}},
                "next_best_action": {
                    "headline": "Keep Triple Shine Grape's page fresh",
                    "first_move": "Monitor for drift.",
                    "tracking_metrics": ["citation rate"],
                },
            },
        ],
    }


def test_per_sku_audit_materializes_one_task_per_sku():
    items = _extract_action_items(_per_sku_report())
    titles = [it["title"] for it in items]
    assert "Fill the gaps on Good Night Collagen's page before chasing reach" in titles
    assert "Keep Triple Shine Grape's page fresh" in titles
    assert len(items) == 2


def test_per_sku_task_carries_product_key_outcome_and_severity():
    by_title = {it["title"]: it for it in _extract_action_items(_per_sku_report())}
    collagen = by_title["Fill the gaps on Good Night Collagen's page before chasing reach"]
    assert collagen["evidence"]["product_key"] == "https://agent.pivota.cc/products/sig_abc"
    # tracking_metrics surface as the success signal (not a bare title)
    assert collagen["evidence"]["expected_outcome"] == "enrichment coverage completeness"
    assert collagen["evidence"]["kpi_to_track"] == "citation rate"
    # low citation -> high severity; healthy SKU -> low
    assert collagen["severity"] == "high"
    grape = by_title["Keep Triple Shine Grape's page fresh"]
    assert grape["severity"] == "low"
    # in-app cta (request_enrichment, no http url) -> no fabricated cta_url
    assert collagen["evidence"]["cta_url"] is None


def test_legacy_per_product_still_takes_precedence():
    """A report carrying per_product is unaffected (no double-count)."""
    report = {
        "per_product": [{
            "product_key": "pk-A",
            "product": {"title": "Widget"},
            "merchant_view": {"actions": [
                {"title": "Do the legacy thing", "lever": "content_revision"}
            ]},
        }],
        "per_sku_reports": [{
            "sku_key": "sku-A", "product_key": "pk-A",
            "next_best_action": {"headline": "Per-SKU thing", "first_move": "x"},
        }],
    }
    titles = [it["title"] for it in _extract_action_items(report)]
    # only the legacy action (product-name-disambiguated per #941); the per_sku
    # branch is skipped because per_product produced output.
    assert titles == ["Do the legacy thing — Widget"]


def test_empty_nba_skipped():
    report = {"per_sku_reports": [
        {"sku_key": "s1", "next_best_action": {"is_empty": True, "headline": "x"}},
        {"sku_key": "s2", "next_best_action": {}},  # no headline
        {"sku_key": "s3"},  # no nba
    ]}
    assert _per_sku_action_items(report, set()) == []
