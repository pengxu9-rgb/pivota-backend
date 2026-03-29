#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two generic commerce shadow audit reports and summarize deploy deltas."
    )
    parser.add_argument("--before-json", required=True)
    parser.add_argument("--after-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--before-label", default="before")
    parser.add_argument("--after-label", default="after")
    return parser.parse_args()


def _load_json(path_str: str) -> Dict[str, Any]:
    payload = json.loads(Path(path_str).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path_str} must be a JSON object")
    return payload


def _case_key(case: Dict[str, Any]) -> Optional[str]:
    if not isinstance(case, dict):
        return None
    case_id = str(case.get("case_id") or "").strip()
    if case_id:
        return case_id
    query = str(case.get("query") or "").strip()
    source = str(case.get("source") or "unknown").strip()
    if query:
        return f"{source}::{query}"
    return None


def _case_map(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    cases = report.get("cases") or []
    if not isinstance(cases, list):
        return {}
    mapped: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        key = _case_key(case)
        if key:
            mapped[key] = case
    return mapped


def _summary_metric(report: Dict[str, Any], key: str) -> Optional[int]:
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    value = summary.get(key)
    return value if isinstance(value, int) else None


def _source_summary(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return {}
    source_summary = summary.get("source_summary")
    return source_summary if isinstance(source_summary, dict) else {}


def _render_md(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Generic Commerce Shadow Audit Compare",
        "",
        f"- before_label: `{report['before_label']}`",
        f"- after_label: `{report['after_label']}`",
        f"- before_top1_matches: `{summary.get('before_top1_matches')}` / `{summary.get('before_top1_evaluable')}`",
        f"- after_top1_matches: `{summary.get('after_top1_matches')}` / `{summary.get('after_top1_evaluable')}`",
        f"- top1_match_delta: `{summary.get('top1_match_delta')}`",
        f"- before_no_result_mismatch_cases: `{summary.get('before_no_result_mismatch_cases')}`",
        f"- after_no_result_mismatch_cases: `{summary.get('after_no_result_mismatch_cases')}`",
        f"- improved_queries: `{summary.get('improved_query_count')}`",
        f"- regressed_queries: `{summary.get('regressed_query_count')}`",
        "",
        "## Source Deltas",
        "",
    ]
    source_summary = summary.get("source_summary") or {}
    if not source_summary:
        lines.append("- none")
    else:
        for source, details in source_summary.items():
            lines.append(
                f"- {source}: before_top1_match_rate=`{details.get('before_top1_match_rate')}` "
                f"after_top1_match_rate=`{details.get('after_top1_match_rate')}` "
                f"top1_match_delta=`{details.get('top1_match_delta')}` "
                f"before_no_result_mismatch=`{details.get('before_no_result_mismatch_cases')}` "
                f"after_no_result_mismatch=`{details.get('after_no_result_mismatch_cases')}`"
            )
    lines.extend(["", "## Query Deltas", ""])
    for case in report.get("cases") or []:
        lines.extend(
            [
                f"### {case['query']} [{case['source']}]",
                "",
                f"- before_top1_same: `{case.get('before_top1_same')}`",
                f"- after_top1_same: `{case.get('after_top1_same')}`",
                f"- before_gateway_top1: `{case.get('before_gateway_top1')}`",
                f"- before_pivot_top1: `{case.get('before_pivot_top1')}`",
                f"- after_gateway_top1: `{case.get('after_gateway_top1')}`",
                f"- after_pivot_top1: `{case.get('after_pivot_top1')}`",
                f"- before_top5_overlap: `{case.get('before_top5_overlap')}`",
                f"- after_top5_overlap: `{case.get('after_top5_overlap')}`",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    before = _load_json(args.before_json)
    after = _load_json(args.after_json)
    before_cases = _case_map(before)
    after_cases = _case_map(after)
    keys = sorted(set(before_cases) | set(after_cases))

    cases: List[Dict[str, Any]] = []
    improved_queries = 0
    regressed_queries = 0
    overlap_gain_cases = 0
    overlap_loss_cases = 0

    for key in keys:
        before_case = before_cases.get(key) or {}
        after_case = after_cases.get(key) or {}
        before_top1_same = before_case.get("top1_same")
        after_top1_same = after_case.get("top1_same")
        before_overlap = before_case.get("top5_overlap")
        after_overlap = after_case.get("top5_overlap")
        if before_top1_same is False and after_top1_same is True:
            improved_queries += 1
        if before_top1_same is True and after_top1_same is False:
            regressed_queries += 1
        if isinstance(before_overlap, int) and isinstance(after_overlap, int):
            if after_overlap > before_overlap:
                overlap_gain_cases += 1
            elif after_overlap < before_overlap:
                overlap_loss_cases += 1
        cases.append(
            {
                "case_id": key,
                "query": str(after_case.get("query") or before_case.get("query") or ""),
                "source": str(after_case.get("source") or before_case.get("source") or "unknown"),
                "before_top1_same": before_top1_same,
                "after_top1_same": after_top1_same,
                "before_gateway_top1": before_case.get("gateway_top1"),
                "before_pivot_top1": before_case.get("pivot_top1"),
                "after_gateway_top1": after_case.get("gateway_top1"),
                "after_pivot_top1": after_case.get("pivot_top1"),
                "before_top5_overlap": before_overlap,
                "after_top5_overlap": after_overlap,
                "before_no_result_mismatch": before_case.get("no_result_mismatch"),
                "after_no_result_mismatch": after_case.get("no_result_mismatch"),
            }
        )

    before_source_summary = _source_summary(before)
    after_source_summary = _source_summary(after)
    source_summary: Dict[str, Dict[str, Any]] = {}
    for source in sorted(set(before_source_summary) | set(after_source_summary)):
        before_details = before_source_summary.get(source) or {}
        after_details = after_source_summary.get(source) or {}
        before_top1_matches = before_details.get("top1_matches")
        after_top1_matches = after_details.get("top1_matches")
        source_summary[source] = {
            "before_top1_matches": before_top1_matches,
            "before_top1_evaluable": before_details.get("top1_evaluable"),
            "after_top1_matches": after_top1_matches,
            "after_top1_evaluable": after_details.get("top1_evaluable"),
            "before_top1_match_rate": before_details.get("top1_match_rate"),
            "after_top1_match_rate": after_details.get("top1_match_rate"),
            "top1_match_delta": (
                (after_top1_matches - before_top1_matches)
                if isinstance(before_top1_matches, int) and isinstance(after_top1_matches, int)
                else None
            ),
            "before_no_result_mismatch_cases": before_details.get("no_result_mismatch_cases"),
            "after_no_result_mismatch_cases": after_details.get("no_result_mismatch_cases"),
        }

    before_top1_matches = _summary_metric(before, "top1_matches")
    after_top1_matches = _summary_metric(after, "top1_matches")
    report = {
        "before_label": args.before_label,
        "after_label": args.after_label,
        "before_json": str(Path(args.before_json).resolve()),
        "after_json": str(Path(args.after_json).resolve()),
        "summary": {
            "before_top1_matches": before_top1_matches,
            "before_top1_evaluable": _summary_metric(before, "top1_evaluable"),
            "after_top1_matches": after_top1_matches,
            "after_top1_evaluable": _summary_metric(after, "top1_evaluable"),
            "top1_match_delta": (
                (after_top1_matches - before_top1_matches)
                if before_top1_matches is not None and after_top1_matches is not None
                else None
            ),
            "before_no_result_mismatch_cases": _summary_metric(before, "no_result_mismatch_cases"),
            "after_no_result_mismatch_cases": _summary_metric(after, "no_result_mismatch_cases"),
            "improved_query_count": improved_queries,
            "regressed_query_count": regressed_queries,
            "overlap_gain_cases": overlap_gain_cases,
            "overlap_loss_cases": overlap_loss_cases,
            "query_count": len(keys),
            "source_summary": source_summary,
        },
        "cases": cases,
    }
    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    output_md = Path(args.output_md)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_render_md(report), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
