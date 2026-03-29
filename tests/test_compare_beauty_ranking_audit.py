from __future__ import annotations

import argparse
import json
from pathlib import Path

import scripts.compare_beauty_ranking_audit as module


def test_compare_beauty_ranking_audit_summarizes_improvements_and_regressions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    before_json = tmp_path / "before.json"
    after_json = tmp_path / "after.json"
    output_json = tmp_path / "compare.json"
    output_md = tmp_path / "compare.md"

    before_json.write_text(
        json.dumps(
            {
                "summary": {
                    "gateway_top1_matches": 3,
                    "gateway_top1_evaluable": 10,
                    "gateway_nonempty": 10,
                },
                "cases": [
                    {
                        "query": "acne cleanser",
                        "top1_diff": {
                            "gateway_vs_ranked": {
                                "ranked_top1": "Acne Control Clarifying Cleanser",
                                "gateway_top1": "Clarifying Cleanser Larger Size",
                                "same": False,
                            }
                        },
                        "top5_overlap": {"gateway_vs_ranked": 3},
                    },
                    {
                        "query": "gentle cleanser",
                        "top1_diff": {
                            "gateway_vs_ranked": {
                                "ranked_top1": "Ultra Gentle Cream-to-Foam Face Cleanser Jumbo",
                                "gateway_top1": "Ultra Gentle Cream-to-Foam Face Cleanser Jumbo",
                                "same": True,
                            }
                        },
                        "top5_overlap": {"gateway_vs_ranked": 4},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    after_json.write_text(
        json.dumps(
            {
                "summary": {
                    "gateway_top1_matches": 4,
                    "gateway_top1_evaluable": 10,
                    "gateway_nonempty": 10,
                },
                "cases": [
                    {
                        "query": "acne cleanser",
                        "top1_diff": {
                            "gateway_vs_ranked": {
                                "ranked_top1": "Acne Control Clarifying Cleanser",
                                "gateway_top1": "Acne Control Clarifying Cleanser",
                                "same": True,
                            }
                        },
                        "top5_overlap": {"gateway_vs_ranked": 5},
                    },
                    {
                        "query": "gentle cleanser",
                        "top1_diff": {
                            "gateway_vs_ranked": {
                                "ranked_top1": "Ultra Gentle Cream-to-Foam Face Cleanser Jumbo",
                                "gateway_top1": "Ultra Gentle Cream-to-Foam Face Cleanser Travel Size",
                                "same": False,
                            }
                        },
                        "top5_overlap": {"gateway_vs_ranked": 3},
                    },
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
        before_label="before-deploy",
        after_label="after-deploy",
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert summary["before_top1_matches"] == 3
    assert summary["after_top1_matches"] == 4
    assert summary["top1_match_delta"] == 1
    assert summary["improved_query_count"] == 1
    assert summary["regressed_query_count"] == 1
    assert summary["overlap_gain_cases"] == 1
    assert summary["overlap_loss_cases"] == 1
    assert output_md.read_text(encoding="utf-8").startswith("# Beauty Ranking Audit Compare")
