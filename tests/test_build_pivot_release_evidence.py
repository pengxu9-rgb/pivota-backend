from __future__ import annotations

import argparse
import json
from pathlib import Path

import scripts.build_pivot_release_evidence as module


def test_build_pivot_release_evidence_distinguishes_blocking_and_legacy_probe(tmp_path: Path, monkeypatch) -> None:
    release_gate = tmp_path / "release-gate.json"
    smoke = tmp_path / "smoke.json"
    probe = tmp_path / "probe.json"
    output_json = tmp_path / "evidence.json"
    output_md = tmp_path / "evidence.md"

    release_gate.write_text(
        json.dumps({"summary": {"failed_cases": 0, "passed_cases": 4}}),
        encoding="utf-8",
    )
    smoke.write_text(json.dumps({"overall_ok": True}), encoding="utf-8")
    probe.write_text(
        json.dumps(
            {
                "records": [
                    {"http_status": 0, "ok": False, "product_count": None},
                    {"http_status": 401, "ok": False, "product_count": None},
                ]
            }
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        migration=None,
        backfill_verify_json=None,
        release_gate_json=str(release_gate),
        catalog_pivot_smoke_json=str(smoke),
        search_chain_probe_json=str(probe),
        output_json=str(output_json),
        output_md=str(output_md),
        label="test-evidence",
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert summary["blocking_ready"] is True
    assert summary["search_chain_probe_present"] is True
    assert summary["search_chain_probe_records_total"] == 2
    assert summary["search_chain_probe_records_ok"] == 0
    assert summary["search_chain_probe_records_http_200"] == 0
    assert summary["search_chain_probe_legacy_parity_ok"] is False


def test_build_pivot_release_evidence_summarizes_beauty_ranking_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    release_gate = tmp_path / "release-gate.json"
    smoke = tmp_path / "smoke.json"
    beauty_audit = tmp_path / "beauty-audit.json"
    beauty_compare = tmp_path / "beauty-compare.json"
    output_json = tmp_path / "evidence.json"
    output_md = tmp_path / "evidence.md"

    release_gate.write_text(
        json.dumps({"summary": {"failed_cases": 0, "passed_cases": 4}}),
        encoding="utf-8",
    )
    smoke.write_text(json.dumps({"overall_ok": True}), encoding="utf-8")
    beauty_audit.write_text(
        json.dumps(
            {
                "summary": {
                    "case_count": 10,
                    "gateway_top1_matches": 7,
                    "gateway_top1_evaluable": 10,
                    "pivot_top1_matches": 6,
                    "pivot_top1_evaluable": 10,
                    "gateway_nonempty": 10,
                    "pivot_nonempty": 10,
                    "raw_seed_available_cases": 10,
                }
            }
        ),
        encoding="utf-8",
    )
    beauty_compare.write_text(
        json.dumps(
            {
                "summary": {
                    "top1_match_delta": 4,
                    "improved_query_count": 4,
                    "regressed_query_count": 0,
                    "overlap_gain_cases": 5,
                    "overlap_loss_cases": 0,
                }
            }
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        migration=None,
        backfill_verify_json=None,
        release_gate_json=str(release_gate),
        catalog_pivot_smoke_json=str(smoke),
        search_chain_probe_json=None,
        beauty_ranking_audit_json=str(beauty_audit),
        beauty_ranking_audit_compare_json=str(beauty_compare),
        output_json=str(output_json),
        output_md=str(output_md),
        label="test-evidence",
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert summary["beauty_ranking_audit_present"] is True
    assert summary["beauty_ranking_case_count"] == 10
    assert summary["beauty_ranking_gateway_top1_matches"] == 7
    assert summary["beauty_ranking_gateway_top1_match_rate"] == 0.7
    assert summary["beauty_ranking_pivot_top1_matches"] == 6
    assert summary["beauty_ranking_compare_present"] is True
    assert summary["beauty_ranking_top1_match_delta"] == 4
    assert summary["beauty_ranking_regressed_query_count"] == 0
    assert summary["beauty_ranking_non_regressing"] is True


def test_build_pivot_release_evidence_summarizes_commerce_shadow_and_source_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    release_gate = tmp_path / "release-gate.json"
    smoke = tmp_path / "smoke.json"
    commerce_audit = tmp_path / "commerce-audit.json"
    commerce_compare = tmp_path / "commerce-compare.json"
    output_json = tmp_path / "evidence.json"
    output_md = tmp_path / "evidence.md"

    release_gate.write_text(
        json.dumps(
            {
                "summary": {
                    "failed_cases": 0,
                    "passed_cases": 6,
                    "source_summary": {
                        "shopping_agent": {
                            "source_stage": "stage_1",
                            "sample_count": 2,
                            "passed_cases": 2,
                            "failed_cases": 0,
                            "rollout_modes": {"shadow": 2},
                        },
                        "shopping-agent-ui": {
                            "source_stage": "stage_2",
                            "sample_count": 2,
                            "passed_cases": 2,
                            "failed_cases": 0,
                            "rollout_modes": {"shadow": 2},
                        },
                    },
                    "semantic_class_summary": {
                        "default": {"sample_count": 3, "passed_cases": 3, "failed_cases": 0},
                        "fragrance": {"sample_count": 3, "passed_cases": 3, "failed_cases": 0},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    smoke.write_text(json.dumps({"overall_ok": True}), encoding="utf-8")
    commerce_audit.write_text(
        json.dumps(
            {
                "summary": {
                    "case_count": 6,
                    "top1_matches": 5,
                    "top1_evaluable": 6,
                    "gateway_nonempty": 6,
                    "pivot_nonempty": 6,
                    "no_result_mismatch_cases": 0,
                    "bad_price_anomaly_cases": 0,
                    "source_summary": {
                        "shopping_agent": {
                            "sample_count": 2,
                            "top1_matches": 2,
                            "top1_evaluable": 2,
                            "top1_match_rate": 1.0,
                            "no_result_mismatch_cases": 0,
                            "bad_price_anomaly_cases": 0,
                        },
                        "shopping-agent-ui": {
                            "sample_count": 2,
                            "top1_matches": 1,
                            "top1_evaluable": 2,
                            "top1_match_rate": 0.5,
                            "no_result_mismatch_cases": 0,
                            "bad_price_anomaly_cases": 0,
                        },
                    },
                    "semantic_class_summary": {
                        "default": {"sample_count": 3, "top1_matches": 2, "top1_evaluable": 3},
                        "fragrance": {"sample_count": 3, "top1_matches": 3, "top1_evaluable": 3},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    commerce_compare.write_text(
        json.dumps(
            {
                "summary": {
                    "top1_match_delta": 2,
                    "improved_query_count": 2,
                    "regressed_query_count": 0,
                    "overlap_gain_cases": 2,
                    "overlap_loss_cases": 0,
                    "source_summary": {
                        "shopping_agent": {"top1_match_delta": 1},
                        "shopping-agent-ui": {"top1_match_delta": 1},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        migration=None,
        backfill_verify_json=None,
        release_gate_json=str(release_gate),
        catalog_pivot_smoke_json=str(smoke),
        search_chain_probe_json=None,
        beauty_ranking_audit_json=None,
        beauty_ranking_audit_compare_json=None,
        commerce_shadow_audit_json=str(commerce_audit),
        commerce_shadow_audit_compare_json=str(commerce_compare),
        output_json=str(output_json),
        output_md=str(output_md),
        label="test-evidence",
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert summary["commerce_shadow_audit_present"] is True
    assert summary["commerce_shadow_case_count"] == 6
    assert summary["commerce_shadow_top1_match_rate"] == 0.8333
    assert summary["commerce_shadow_compare_present"] is True
    assert summary["commerce_shadow_top1_match_delta"] == 2
    assert summary["source_stage"] == "shopping-agent-ui"
    assert summary["serve_readiness_by_source"]["shopping_agent"]["ready"] is True
    assert summary["serve_readiness_by_source"]["shopping_agent"]["source_stage"] == "stage_1"
    assert summary["semantic_class_summary"]["commerce_shadow"]["fragrance"]["sample_count"] == 3
