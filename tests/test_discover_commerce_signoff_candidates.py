from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.discover_commerce_signoff_candidates as module  # noqa: E402


def _build_args(tmp_path: Path, cohort_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        database_url="postgresql://example/db",
        cohort=str(cohort_path),
        limit=50,
        output_json=str(tmp_path / "discovery.json"),
        output_md=str(tmp_path / "discovery.md"),
    )


def test_build_candidate_marks_live_eligible() -> None:
    candidate = module._build_candidate(
        {
            "merchant_id": "merch_1",
            "products_cache_rows": 10,
            "titled_rows": 10,
            "sample_title": "Winona Serum",
            "latest_cached_at": "2026-03-30 01:00:00",
            "active_psp_rows": 2,
            "active_psp_providers": ["stripe", "adyen"],
            "active_psp_environments": ["live"],
            "psp_statuses": ["active"],
            "active_psp_records": [
                {
                    "provider": "stripe",
                    "status": "active",
                    "api_key": "sk_live_123",
                    "account_id": None,
                    "provider_config": {
                        "mode": "payment_intent",
                        "public_key": "pk_live_123",
                        "webhook_endpoint_id": "we_123",
                        "webhook_endpoint_secret": "whsec_123",
                    },
                    "environment": "live",
                    "validation_status": "valid",
                    "validation_error": None,
                }
            ],
            "catalog_offer_rows": 25,
            "currencies": ["USD"],
        },
        {
            "case_id": "beauty_1",
            "label": "Beauty",
            "semantic_class": "beauty",
            "enabled": True,
        },
    )

    assert candidate["live_eligible"] is True
    assert candidate["has_live_ready_supported_psp"] is True
    assert candidate["live_ready_supported_psp_providers"] == ["stripe"]
    assert candidate["gap_reasons"] == []
    assert candidate["candidate_query"] == "Winona Serum"
    assert candidate["cohort_case_id"] == "beauty_1"


def test_build_report_counts_gaps_and_capacity() -> None:
    report = module._build_report(
        [
            {
                "merchant_id": "merch_1",
                "products_cache_rows": 10,
                "titled_rows": 10,
                "sample_title": "Winona Serum",
                "latest_cached_at": "2026-03-30 01:00:00",
                "active_psp_rows": 1,
                "active_psp_providers": ["stripe"],
                "active_psp_environments": ["live"],
                "psp_statuses": ["active"],
                "active_psp_records": [
                    {
                        "provider": "stripe",
                        "status": "active",
                        "api_key": "sk_live_123",
                        "account_id": None,
                        "provider_config": {
                            "mode": "payment_intent",
                            "public_key": "pk_live_123",
                            "webhook_endpoint_id": "we_123",
                            "webhook_endpoint_secret": "whsec_123",
                        },
                        "environment": "live",
                        "validation_status": "valid",
                        "validation_error": None,
                    }
                ],
                "catalog_offer_rows": 25,
                "currencies": ["USD"],
            },
            {
                "merchant_id": "merch_2",
                "products_cache_rows": 0,
                "titled_rows": 0,
                "sample_title": None,
                "latest_cached_at": None,
                "active_psp_rows": 1,
                "active_psp_providers": ["stripe"],
                "active_psp_environments": ["test"],
                "psp_statuses": ["active"],
                "active_psp_records": [
                    {
                        "provider": "stripe",
                        "status": "active",
                        "api_key": "sk_test_123",
                        "account_id": None,
                        "provider_config": {"mode": "payment_intent"},
                        "environment": "test",
                        "validation_status": "valid",
                        "validation_error": None,
                    }
                ],
                "catalog_offer_rows": 0,
                "currencies": [],
            },
        ],
        {
            "cohort_name": "cohort_a",
            "min_enabled_cases": 1,
            "target_enabled_cases": 3,
            "cases": [
                {
                    "case_id": "beauty_1",
                    "merchant_id": "merch_1",
                    "semantic_class": "beauty",
                    "enabled": True,
                }
            ],
        },
    )

    summary = report["summary"]
    assert summary["live_eligible_merchants"] == 1
    assert summary["merchants_with_live_ready_supported_psp"] == 1
    assert summary["enough_capacity_for_min_gate"] is True
    assert summary["enough_capacity_for_target_gate"] is False
    assert summary["missing_capacity_to_target"] == 2
    assert summary["gap_reason_counts"] == {
        "missing_catalog_offers": 1,
        "missing_live_ready_supported_psp": 1,
        "missing_products_cache": 1,
    }


def test_main_writes_json_and_markdown(monkeypatch, tmp_path: Path) -> None:
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(
        json.dumps(
            {
                "cohort_name": "cohort_b",
                "min_enabled_cases": 1,
                "target_enabled_cases": 2,
                "cases": [
                    {
                        "case_id": "beauty_1",
                        "merchant_id": "merch_1",
                        "semantic_class": "beauty",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_fetch_merchant_rows",
        lambda database_url, limit: [
            {
                "merchant_id": "merch_1",
                "products_cache_rows": 10,
                "titled_rows": 10,
                "sample_title": "Winona Serum",
                "latest_cached_at": "2026-03-30 01:00:00",
                "active_psp_rows": 1,
                "active_psp_providers": ["stripe"],
                "active_psp_environments": ["live"],
                "psp_statuses": ["active"],
                "active_psp_records": [
                    {
                        "provider": "stripe",
                        "status": "active",
                        "api_key": "sk_live_123",
                        "account_id": None,
                        "provider_config": {
                            "mode": "payment_intent",
                            "public_key": "pk_live_123",
                            "webhook_endpoint_id": "we_123",
                            "webhook_endpoint_secret": "whsec_123",
                        },
                        "environment": "live",
                        "validation_status": "valid",
                        "validation_error": None,
                    }
                ],
                "catalog_offer_rows": 25,
                "currencies": ["USD"],
            }
        ],
    )
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, cohort_path))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "discovery.json").read_text(encoding="utf-8"))
    assert payload["summary"]["live_eligible_merchants"] == 1
    markdown = (tmp_path / "discovery.md").read_text(encoding="utf-8")
    assert "Commerce Signoff Candidate Discovery" in markdown
    assert "merch_1" in markdown
