from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.run_commerce_channels_signoff_batch as module  # noqa: E402


def _build_args(tmp_path: Path, cohort_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        base_url="https://api.example",
        database_url="postgresql://example/db",
        cohort=str(cohort_path),
        header=["Authorization: Bearer test"],
        internal_key="internal-test-key",
        timeout_seconds=10.0,
        sync_wait_seconds=20.0,
        sync_poll_interval_seconds=2.0,
        backfill_timeout_seconds=60.0,
        output_dir=str(tmp_path / "cases"),
        output_json=str(tmp_path / "batch.json"),
        output_md=str(tmp_path / "batch.md"),
        case_id=[],
        min_enabled_cases=None,
    )


def test_batch_runner_passes_current_gate_when_only_long_term_target_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(
        json.dumps(
            {
                "cohort_name": "cohort_a",
                "min_enabled_cases": 1,
                "target_enabled_cases": 3,
                "required_semantic_classes": ["beauty"],
                "target_semantic_classes": ["beauty", "generic_default"],
                "cases": [
                    {
                        "case_id": "beauty_1",
                        "merchant_id": "merch_1",
                        "semantic_class": "beauty",
                        "enabled": True,
                    },
                    {
                        "case_id": "generic_pending",
                        "merchant_id": "merch_2",
                        "semantic_class": "generic_default",
                        "enabled": False,
                        "skip_reason": "missing_products_cache_live_payload",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        module,
        "_run_case_signoff",
        lambda case, args, case_output_dir: {
            "case_id": case["case_id"],
            "merchant_id": case["merchant_id"],
            "semantic_class": case["semantic_class"],
            "enabled": True,
            "ok": True,
            "returncode": 0,
            "payload": {"overall_ok": True},
        },
    )
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, cohort_path))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "batch.json").read_text(encoding="utf-8"))
    assert payload["overall_ok"] is True
    assert payload["summary"]["enabled_cases"] == 1
    assert payload["summary"]["meets_min_enabled_cases"] is True
    assert payload["summary"]["meets_target_enabled_cases"] is False
    assert payload["summary"]["missing_semantic_classes"] == []
    assert payload["summary"]["missing_target_semantic_classes"] == ["generic_default"]
    assert payload["summary"]["skipped_reason_counts"] == {"missing_products_cache_live_payload": 1}


def test_batch_runner_passes_when_cases_and_coverage_are_satisfied(monkeypatch, tmp_path: Path) -> None:
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(
        json.dumps(
            {
                "cohort_name": "cohort_b",
                "min_enabled_cases": 1,
                "target_enabled_cases": 2,
                "required_semantic_classes": ["beauty"],
                "target_semantic_classes": ["beauty", "generic_default"],
                "cases": [
                    {
                        "case_id": "beauty_1",
                        "merchant_id": "merch_1",
                        "semantic_class": "beauty",
                        "enabled": True,
                    },
                    {
                        "case_id": "generic_1",
                        "merchant_id": "merch_2",
                        "semantic_class": "generic_default",
                        "enabled": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        module,
        "_run_case_signoff",
        lambda case, args, case_output_dir: {
            "case_id": case["case_id"],
            "merchant_id": case["merchant_id"],
            "semantic_class": case["semantic_class"],
            "enabled": True,
            "ok": True,
            "returncode": 0,
            "payload": {"overall_ok": True},
        },
    )
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, cohort_path))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "batch.json").read_text(encoding="utf-8"))
    assert payload["overall_ok"] is True
    assert payload["summary"]["passed_cases"] == 2
    assert payload["summary"]["missing_semantic_classes"] == []
    assert payload["summary"]["missing_target_semantic_classes"] == []


def test_batch_runner_supports_case_filter(monkeypatch, tmp_path: Path) -> None:
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(
        json.dumps(
            {
                "cohort_name": "cohort_c",
                "min_enabled_cases": 1,
                "target_enabled_cases": 1,
                "required_semantic_classes": ["beauty"],
                "target_semantic_classes": ["beauty"],
                "cases": [
                    {
                        "case_id": "beauty_1",
                        "merchant_id": "merch_1",
                        "semantic_class": "beauty",
                        "enabled": True,
                    },
                    {
                        "case_id": "generic_1",
                        "merchant_id": "merch_2",
                        "semantic_class": "generic_default",
                        "enabled": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    calls = []

    def _fake_run(case, args, case_output_dir):
        calls.append(case["case_id"])
        return {
            "case_id": case["case_id"],
            "merchant_id": case["merchant_id"],
            "semantic_class": case["semantic_class"],
            "enabled": True,
            "ok": True,
            "returncode": 0,
            "payload": {"overall_ok": True},
        }

    args = _build_args(tmp_path, cohort_path)
    args.case_id = ["beauty_1"]
    monkeypatch.setattr(module, "_run_case_signoff", _fake_run)
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    assert calls == ["beauty_1"]
    payload = json.loads((tmp_path / "batch.json").read_text(encoding="utf-8"))
    assert payload["summary"]["total_cases"] == 1


def test_batch_runner_reports_target_platform_and_target_domain_gaps_without_failing_current_gate(
    monkeypatch, tmp_path: Path
) -> None:
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(
        json.dumps(
            {
                "cohort_name": "cohort_d",
                "min_enabled_cases": 1,
                "target_enabled_cases": 5,
                "required_semantic_classes": ["beauty"],
                "target_semantic_classes": ["beauty", "generic_default", "fragrance"],
                "target_platform_counts": {"shopify": 3, "wix": 2},
                "target_ready_domains": ["foundation", "discover", "signals", "execute"],
                "cases": [
                    {
                        "case_id": "beauty_1",
                        "merchant_id": "merch_1",
                        "semantic_class": "beauty",
                        "enabled": True,
                    },
                    {
                        "case_id": "generic_1",
                        "merchant_id": "merch_2",
                        "semantic_class": "generic_default",
                        "enabled": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def _fake_run(case, args, case_output_dir):
        if case["case_id"] == "beauty_1":
            return {
                "case_id": case["case_id"],
                "merchant_id": case["merchant_id"],
                "semantic_class": case["semantic_class"],
                "catalog_platform": "shopify",
                "readiness_state_available": True,
                "readiness_domains": {
                    "foundation": "ready",
                    "discover": "ready",
                    "signals": "ready",
                    "execute": "ready",
                },
                "enabled": True,
                "ok": True,
                "returncode": 0,
                "payload": {"overall_ok": True},
            }
        return {
            "case_id": case["case_id"],
            "merchant_id": case["merchant_id"],
            "semantic_class": case["semantic_class"],
            "catalog_platform": "shopify",
            "readiness_state_available": True,
            "readiness_domains": {
                "foundation": "ready",
                "discover": "ready",
                "signals": "blocked",
                "execute": "ready",
            },
            "enabled": True,
            "ok": True,
            "returncode": 0,
            "payload": {"overall_ok": True},
        }

    monkeypatch.setattr(module, "_run_case_signoff", _fake_run)
    monkeypatch.setattr(module, "_parse_args", lambda: _build_args(tmp_path, cohort_path))

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "batch.json").read_text(encoding="utf-8"))
    assert payload["overall_ok"] is True
    assert payload["summary"]["platform_summary"] == {"shopify": 2}
    assert payload["summary"]["missing_target_platform_counts"] == {"shopify": 1, "wix": 2}
    assert payload["summary"]["meets_target_platform_counts"] is False
    assert payload["summary"]["all_enabled_cases_ready_for_target_domains"] is False
    assert payload["summary"]["target_domain_failures"]["signals"] == ["generic_1"]
