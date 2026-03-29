from __future__ import annotations

import argparse
import json
from pathlib import Path

import scripts.compare_commerce_shadow_audit as module


def test_compare_commerce_shadow_audit_summarizes_source_deltas(
    tmp_path: Path, monkeypatch
) -> None:
    before_json = tmp_path / "before.json"
    after_json = tmp_path / "after.json"
    output_json = tmp_path / "compare.json"
    output_md = tmp_path / "compare.md"

    before_json.write_text(
        json.dumps(
            {
                "summary": {
                    "top1_matches": 1,
                    "top1_evaluable": 2,
                    "no_result_mismatch_cases": 1,
                    "source_summary": {
                        "shopping_agent": {
                            "top1_matches": 1,
                            "top1_evaluable": 2,
                            "top1_match_rate": 0.5,
                            "no_result_mismatch_cases": 1,
                        }
                    },
                },
                "cases": [
                    {
                        "case_id": "shopping_agent::rose eau de parfum",
                        "query": "rose eau de parfum",
                        "source": "shopping_agent",
                        "top1_same": False,
                        "gateway_top1": "A",
                        "pivot_top1": "B",
                        "top5_overlap": 1,
                        "no_result_mismatch": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    after_json.write_text(
        json.dumps(
            {
                "summary": {
                    "top1_matches": 2,
                    "top1_evaluable": 2,
                    "no_result_mismatch_cases": 0,
                    "source_summary": {
                        "shopping_agent": {
                            "top1_matches": 2,
                            "top1_evaluable": 2,
                            "top1_match_rate": 1.0,
                            "no_result_mismatch_cases": 0,
                        }
                    },
                },
                "cases": [
                    {
                        "case_id": "shopping_agent::rose eau de parfum",
                        "query": "rose eau de parfum",
                        "source": "shopping_agent",
                        "top1_same": True,
                        "gateway_top1": "A",
                        "pivot_top1": "A",
                        "top5_overlap": 3,
                        "no_result_mismatch": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        before_json=str(before_json),
        after_json=str(after_json),
        output_json=str(output_json),
        output_md=str(output_md),
        before_label="before",
        after_label="after",
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["summary"]["top1_match_delta"] == 1
    assert payload["summary"]["regressed_query_count"] == 0
    assert payload["summary"]["source_summary"]["shopping_agent"]["top1_match_delta"] == 1
    assert payload["summary"]["source_summary"]["shopping_agent"]["after_no_result_mismatch_cases"] == 0
