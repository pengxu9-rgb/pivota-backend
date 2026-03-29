#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two beauty ranking audit reports and summarize deploy deltas."
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


def _case_map(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    cases = report.get("cases") or []
    if not isinstance(cases, list):
        return {}
    mapped: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        query = str(case.get("query") or "").strip()
        if query:
            mapped[query] = case
    return mapped


def _gateway_top1_same(case: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not isinstance(case, dict):
        return None
    diff = case.get("top1_diff")
    if isinstance(diff, dict):
        gateway = diff.get("gateway_vs_ranked")
        if isinstance(gateway, dict):
            value = gateway.get("same")
            if isinstance(value, bool):
                return value
    value = case.get("top1_same")
    return value if isinstance(value, bool) else None


def _gateway_top1(case: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(case, dict):
        return None
    diff = case.get("top1_diff")
    if isinstance(diff, dict):
        gateway = diff.get("gateway_vs_ranked")
        if isinstance(gateway, dict):
            title = gateway.get("gateway_top1")
            if isinstance(title, str):
                return title
    ranked = case.get("gateway_top1")
    return ranked if isinstance(ranked, str) else None


def _ranked_top1(case: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(case, dict):
        return None
    diff = case.get("top1_diff")
    if isinstance(diff, dict):
        gateway = diff.get("gateway_vs_ranked")
        if isinstance(gateway, dict):
            title = gateway.get("ranked_top1")
            if isinstance(title, str):
                return title
    ranked = case.get("ranked_top1")
    return ranked if isinstance(ranked, str) else None


def _top5_overlap(case: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(case, dict):
        return None
    overlap = case.get("top5_overlap")
    if isinstance(overlap, dict):
        value = overlap.get("gateway_vs_ranked")
        if isinstance(value, int):
            return value
    value = case.get("top5_overlap")
    return value if isinstance(value, int) else None


def _summary_metric(report: Dict[str, Any], key: str) -> Optional[int]:
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    value = summary.get(key)
    return value if isinstance(value, int) else None


def _render_md(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Beauty Ranking Audit Compare",
        "",
        f"- before_label: `{report['before_label']}`",
        f"- after_label: `{report['after_label']}`",
        f"- before_top1_matches: `{summary.get('before_top1_matches')}` / `{summary.get('before_top1_evaluable')}`",
        f"- after_top1_matches: `{summary.get('after_top1_matches')}` / `{summary.get('after_top1_evaluable')}`",
        f"- top1_match_delta: `{summary.get('top1_match_delta')}`",
        f"- overlap_gain_cases: `{summary.get('overlap_gain_cases')}`",
        f"- overlap_loss_cases: `{summary.get('overlap_loss_cases')}`",
        f"- improved_queries: `{summary.get('improved_query_count')}`",
        f"- regressed_queries: `{summary.get('regressed_query_count')}`",
        "",
        "## Query Deltas",
        "",
    ]
    for case in report.get("cases") or []:
        lines.extend(
            [
                f"### {case['query']}",
                "",
                f"- before_top1_same: `{case.get('before_top1_same')}`",
                f"- after_top1_same: `{case.get('after_top1_same')}`",
                f"- before_ranked_top1: `{case.get('before_ranked_top1')}`",
                f"- before_gateway_top1: `{case.get('before_gateway_top1')}`",
                f"- after_ranked_top1: `{case.get('after_ranked_top1')}`",
                f"- after_gateway_top1: `{case.get('after_gateway_top1')}`",
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
    queries = sorted(set(before_cases) | set(after_cases))

    cases: List[Dict[str, Any]] = []
    improved_queries = 0
    regressed_queries = 0
    overlap_gain_cases = 0
    overlap_loss_cases = 0

    for query in queries:
        before_case = before_cases.get(query)
        after_case = after_cases.get(query)
        before_top1_same = _gateway_top1_same(before_case)
        after_top1_same = _gateway_top1_same(after_case)
        before_overlap = _top5_overlap(before_case)
        after_overlap = _top5_overlap(after_case)

        if before_top1_same is False and after_top1_same is True:
            improved_queries += 1
        if before_top1_same is True and after_top1_same is False:
            regressed_queries += 1
        if before_overlap is not None and after_overlap is not None:
            if after_overlap > before_overlap:
                overlap_gain_cases += 1
            elif after_overlap < before_overlap:
                overlap_loss_cases += 1

        cases.append(
            {
                "query": query,
                "before_top1_same": before_top1_same,
                "after_top1_same": after_top1_same,
                "before_ranked_top1": _ranked_top1(before_case),
                "before_gateway_top1": _gateway_top1(before_case),
                "after_ranked_top1": _ranked_top1(after_case),
                "after_gateway_top1": _gateway_top1(after_case),
                "before_top5_overlap": before_overlap,
                "after_top5_overlap": after_overlap,
            }
        )

    before_top1_matches = _summary_metric(before, "gateway_top1_matches")
    after_top1_matches = _summary_metric(after, "gateway_top1_matches")
    report = {
        "before_label": args.before_label,
        "after_label": args.after_label,
        "before_json": str(Path(args.before_json).resolve()),
        "after_json": str(Path(args.after_json).resolve()),
        "summary": {
            "before_top1_matches": before_top1_matches,
            "before_top1_evaluable": _summary_metric(before, "gateway_top1_evaluable"),
            "after_top1_matches": after_top1_matches,
            "after_top1_evaluable": _summary_metric(after, "gateway_top1_evaluable"),
            "top1_match_delta": (
                (after_top1_matches - before_top1_matches)
                if before_top1_matches is not None and after_top1_matches is not None
                else None
            ),
            "before_gateway_nonempty": _summary_metric(before, "gateway_nonempty"),
            "after_gateway_nonempty": _summary_metric(after, "gateway_nonempty"),
            "improved_query_count": improved_queries,
            "regressed_query_count": regressed_queries,
            "overlap_gain_cases": overlap_gain_cases,
            "overlap_loss_cases": overlap_loss_cases,
            "query_count": len(queries),
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
