#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a single JSON/Markdown release evidence bundle for Celestial Pivot rollout."
    )
    parser.add_argument("--migration", default=None, help="Path to migration verification JSON or text artifact.")
    parser.add_argument("--backfill-verify-json", default=None)
    parser.add_argument("--release-gate-json", default=None)
    parser.add_argument("--catalog-pivot-smoke-json", default=None)
    parser.add_argument("--search-chain-probe-json", default=None)
    parser.add_argument("--beauty-ranking-audit-json", default=None)
    parser.add_argument("--beauty-ranking-audit-compare-json", default=None)
    parser.add_argument("--commerce-shadow-audit-json", default=None)
    parser.add_argument("--commerce-shadow-audit-compare-json", default=None)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--label", default="celestial-pivot-production-ready")
    return parser.parse_args()


def _load_optional_json(path_str: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return {"missing": True, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("_path", str(path))
            return payload
        return {"raw": payload, "_path": str(path)}
    except Exception:
        return {"raw_text": path.read_text(encoding="utf-8")[:4000], "_path": str(path)}


def _render_md(report: Dict[str, Any]) -> str:
    lines = [
        "# Celestial Pivot Release Evidence",
        "",
        f"- label: `{report['label']}`",
        "",
        "## Included Artifacts",
        "",
    ]
    for key, value in report["artifacts"].items():
        path = None
        if isinstance(value, dict):
            path = value.get("_path") or value.get("path")
        lines.append(f"- {key}: `{path or 'not_provided'}`")

    lines.extend(["", "## Summary", ""])
    summary = report.get("summary") or {}
    for key, value in summary.items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _summarize_search_chain_probe(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "present": False,
            "records_total": 0,
            "records_ok": 0,
            "records_http_200": 0,
            "records_nonempty": 0,
            "legacy_parity_ok": None,
        }

    records = payload.get("records") or []
    if not isinstance(records, list):
        records = []
    records_total = len(records)
    records_ok = sum(1 for record in records if bool(record.get("ok")))
    records_http_200 = sum(1 for record in records if int(record.get("http_status") or 0) == 200)
    records_nonempty = sum(1 for record in records if int(record.get("product_count") or 0) > 0)
    return {
        "present": True,
        "records_total": records_total,
        "records_ok": records_ok,
        "records_http_200": records_http_200,
        "records_nonempty": records_nonempty,
        "legacy_parity_ok": records_total > 0 and records_ok == records_total,
    }


def _summarize_beauty_ranking_audit(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "present": False,
            "case_count": 0,
            "gateway_top1_matches": None,
            "gateway_top1_evaluable": None,
            "gateway_top1_match_rate": None,
            "pivot_top1_matches": None,
            "pivot_top1_evaluable": None,
            "pivot_top1_match_rate": None,
            "gateway_nonempty": None,
            "pivot_nonempty": None,
            "raw_seed_available_cases": None,
        }

    summary = payload.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}

    def _match_rate(matches_key: str, evaluable_key: str) -> Optional[float]:
        matches = summary.get(matches_key)
        evaluable = summary.get(evaluable_key)
        if not isinstance(matches, int) or not isinstance(evaluable, int) or evaluable <= 0:
            return None
        return round(matches / evaluable, 4)

    return {
        "present": True,
        "case_count": summary.get("case_count"),
        "gateway_top1_matches": summary.get("gateway_top1_matches"),
        "gateway_top1_evaluable": summary.get("gateway_top1_evaluable"),
        "gateway_top1_match_rate": _match_rate("gateway_top1_matches", "gateway_top1_evaluable"),
        "pivot_top1_matches": summary.get("pivot_top1_matches"),
        "pivot_top1_evaluable": summary.get("pivot_top1_evaluable"),
        "pivot_top1_match_rate": _match_rate("pivot_top1_matches", "pivot_top1_evaluable"),
        "gateway_nonempty": summary.get("gateway_nonempty"),
        "pivot_nonempty": summary.get("pivot_nonempty"),
        "raw_seed_available_cases": summary.get("raw_seed_available_cases"),
    }


def _summarize_beauty_ranking_compare(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "present": False,
            "top1_match_delta": None,
            "improved_query_count": None,
            "regressed_query_count": None,
            "overlap_gain_cases": None,
            "overlap_loss_cases": None,
            "non_regressing": None,
        }

    summary = payload.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    regressed_query_count = summary.get("regressed_query_count")
    return {
        "present": True,
        "top1_match_delta": summary.get("top1_match_delta"),
        "improved_query_count": summary.get("improved_query_count"),
        "regressed_query_count": regressed_query_count,
        "overlap_gain_cases": summary.get("overlap_gain_cases"),
        "overlap_loss_cases": summary.get("overlap_loss_cases"),
        "non_regressing": (regressed_query_count == 0) if isinstance(regressed_query_count, int) else None,
    }


def _summarize_commerce_shadow_audit(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "present": False,
            "case_count": 0,
            "top1_matches": None,
            "top1_evaluable": None,
            "top1_match_rate": None,
            "gateway_nonempty": None,
            "pivot_nonempty": None,
            "no_result_mismatch_cases": None,
            "bad_price_anomaly_cases": None,
            "source_summary": {},
            "semantic_class_summary": {},
        }

    summary = payload.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    top1_matches = summary.get("top1_matches")
    top1_evaluable = summary.get("top1_evaluable")
    top1_match_rate: Optional[float] = None
    if isinstance(top1_matches, int) and isinstance(top1_evaluable, int) and top1_evaluable > 0:
        top1_match_rate = round(top1_matches / top1_evaluable, 4)
    return {
        "present": True,
        "case_count": summary.get("case_count"),
        "top1_matches": top1_matches,
        "top1_evaluable": top1_evaluable,
        "top1_match_rate": top1_match_rate,
        "gateway_nonempty": summary.get("gateway_nonempty"),
        "pivot_nonempty": summary.get("pivot_nonempty"),
        "no_result_mismatch_cases": summary.get("no_result_mismatch_cases"),
        "bad_price_anomaly_cases": summary.get("bad_price_anomaly_cases"),
        "source_summary": summary.get("source_summary") if isinstance(summary.get("source_summary"), dict) else {},
        "semantic_class_summary": summary.get("semantic_class_summary") if isinstance(summary.get("semantic_class_summary"), dict) else {},
    }


def _summarize_commerce_shadow_compare(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "present": False,
            "top1_match_delta": None,
            "improved_query_count": None,
            "regressed_query_count": None,
            "overlap_gain_cases": None,
            "overlap_loss_cases": None,
            "non_regressing": None,
            "source_summary": {},
        }

    summary = payload.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    regressed_query_count = summary.get("regressed_query_count")
    return {
        "present": True,
        "top1_match_delta": summary.get("top1_match_delta"),
        "improved_query_count": summary.get("improved_query_count"),
        "regressed_query_count": regressed_query_count,
        "overlap_gain_cases": summary.get("overlap_gain_cases"),
        "overlap_loss_cases": summary.get("overlap_loss_cases"),
        "non_regressing": (regressed_query_count == 0) if isinstance(regressed_query_count, int) else None,
        "source_summary": summary.get("source_summary") if isinstance(summary.get("source_summary"), dict) else {},
    }


def _serve_readiness_by_source(
    *,
    release_gate_source_summary: Dict[str, Any],
    commerce_shadow_source_summary: Dict[str, Any],
    commerce_shadow_compare_source_summary: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    stage_labels = {
        "shopping_agent": "stage_1",
        "shopping-agent-ui": "stage_2",
        "shopping-agent-web": "stage_3",
    }
    readiness: Dict[str, Dict[str, Any]] = {}
    for source in sorted(set(release_gate_source_summary) | set(commerce_shadow_source_summary)):
        release_gate = (
            release_gate_source_summary.get(source)
            if isinstance(release_gate_source_summary.get(source), dict)
            else {}
        )
        commerce_shadow = (
            commerce_shadow_source_summary.get(source)
            if isinstance(commerce_shadow_source_summary.get(source), dict)
            else {}
        )
        compare_shadow = (
            commerce_shadow_compare_source_summary.get(source)
            if isinstance(commerce_shadow_compare_source_summary.get(source), dict)
            else {}
        )
        release_gate_failed_cases = release_gate.get("failed_cases")
        no_result_mismatch_cases = commerce_shadow.get("no_result_mismatch_cases")
        bad_price_anomaly_cases = commerce_shadow.get("bad_price_anomaly_cases")
        compare_delta = compare_shadow.get("top1_match_delta")
        ready = None
        if release_gate or commerce_shadow:
            ready = bool(
                release_gate.get("sample_count")
                and release_gate_failed_cases == 0
                and (no_result_mismatch_cases in (0, None))
                and (bad_price_anomaly_cases in (0, None))
                and (compare_delta is None or compare_delta >= 0)
            )
        readiness[source] = {
            "source_stage": stage_labels.get(source),
            "release_gate_sample_count": release_gate.get("sample_count"),
            "release_gate_passed_cases": release_gate.get("passed_cases"),
            "release_gate_failed_cases": release_gate_failed_cases,
            "release_gate_rollout_modes": release_gate.get("rollout_modes"),
            "commerce_shadow_sample_count": commerce_shadow.get("sample_count"),
            "commerce_shadow_top1_match_rate": commerce_shadow.get("top1_match_rate"),
            "commerce_shadow_no_result_mismatch_cases": no_result_mismatch_cases,
            "commerce_shadow_bad_price_anomaly_cases": bad_price_anomaly_cases,
            "commerce_shadow_compare_top1_match_delta": compare_delta,
            "ready": ready,
        }
    return readiness


def _source_stage(serve_readiness_by_source: Dict[str, Dict[str, Any]]) -> Optional[str]:
    ordered_sources = ["shopping_agent", "shopping-agent-ui", "shopping-agent-web"]
    last_present: Optional[str] = None
    for source in ordered_sources:
        details = serve_readiness_by_source.get(source) or {}
        if details.get("release_gate_sample_count") or details.get("commerce_shadow_sample_count"):
            last_present = source
    return last_present


def main() -> int:
    args = _parse_args()
    artifacts = {
        "migration": _load_optional_json(getattr(args, "migration", None)),
        "backfill_verify": _load_optional_json(getattr(args, "backfill_verify_json", None)),
        "release_gate": _load_optional_json(getattr(args, "release_gate_json", None)),
        "catalog_pivot_smoke": _load_optional_json(getattr(args, "catalog_pivot_smoke_json", None)),
        "search_chain_probe": _load_optional_json(getattr(args, "search_chain_probe_json", None)),
        "beauty_ranking_audit": _load_optional_json(getattr(args, "beauty_ranking_audit_json", None)),
        "beauty_ranking_audit_compare": _load_optional_json(getattr(args, "beauty_ranking_audit_compare_json", None)),
        "commerce_shadow_audit": _load_optional_json(getattr(args, "commerce_shadow_audit_json", None)),
        "commerce_shadow_audit_compare": _load_optional_json(getattr(args, "commerce_shadow_audit_compare_json", None)),
    }

    release_gate_summary = ((artifacts.get("release_gate") or {}).get("summary") or {}) if isinstance(artifacts.get("release_gate"), dict) else {}
    smoke_ok = bool((artifacts.get("catalog_pivot_smoke") or {}).get("overall_ok")) if isinstance(artifacts.get("catalog_pivot_smoke"), dict) else False
    backfill_summary = ((artifacts.get("backfill_verify") or {}).get("summary") or {}) if isinstance(artifacts.get("backfill_verify"), dict) else {}
    search_chain_summary = _summarize_search_chain_probe(artifacts.get("search_chain_probe"))
    beauty_ranking_summary = _summarize_beauty_ranking_audit(artifacts.get("beauty_ranking_audit"))
    beauty_ranking_compare_summary = _summarize_beauty_ranking_compare(artifacts.get("beauty_ranking_audit_compare"))
    commerce_shadow_summary = _summarize_commerce_shadow_audit(artifacts.get("commerce_shadow_audit"))
    commerce_shadow_compare_summary = _summarize_commerce_shadow_compare(artifacts.get("commerce_shadow_audit_compare"))
    release_gate_failed_cases = release_gate_summary.get("failed_cases")
    release_gate_source_summary = (
        release_gate_summary.get("source_summary")
        if isinstance(release_gate_summary.get("source_summary"), dict)
        else {}
    )
    release_gate_semantic_class_summary = (
        release_gate_summary.get("semantic_class_summary")
        if isinstance(release_gate_summary.get("semantic_class_summary"), dict)
        else {}
    )
    serve_readiness_by_source = _serve_readiness_by_source(
        release_gate_source_summary=release_gate_source_summary,
        commerce_shadow_source_summary=commerce_shadow_summary["source_summary"],
        commerce_shadow_compare_source_summary=commerce_shadow_compare_summary["source_summary"],
    )
    blocking_ready = (release_gate_failed_cases in (0, None)) and smoke_ok

    report = {
        "label": args.label,
        "artifacts": artifacts,
        "summary": {
            "blocking_ready": blocking_ready,
            "source_stage": _source_stage(serve_readiness_by_source),
            "release_gate_failed_cases": release_gate_failed_cases,
            "release_gate_passed_cases": release_gate_summary.get("passed_cases"),
            "release_gate_source_summary": release_gate_source_summary,
            "release_gate_semantic_class_summary": release_gate_semantic_class_summary,
            "catalog_pivot_smoke_ok": smoke_ok,
            "backfill_missing_product_keys_count": ((backfill_summary.get("verify") or {}).get("missing_product_keys_count") if isinstance(backfill_summary.get("verify"), dict) else None),
            "search_chain_probe_present": search_chain_summary["present"],
            "search_chain_probe_records_total": search_chain_summary["records_total"],
            "search_chain_probe_records_ok": search_chain_summary["records_ok"],
            "search_chain_probe_records_http_200": search_chain_summary["records_http_200"],
            "search_chain_probe_records_nonempty": search_chain_summary["records_nonempty"],
            "search_chain_probe_legacy_parity_ok": search_chain_summary["legacy_parity_ok"],
            "beauty_ranking_audit_present": beauty_ranking_summary["present"],
            "beauty_ranking_case_count": beauty_ranking_summary["case_count"],
            "beauty_ranking_gateway_top1_matches": beauty_ranking_summary["gateway_top1_matches"],
            "beauty_ranking_gateway_top1_evaluable": beauty_ranking_summary["gateway_top1_evaluable"],
            "beauty_ranking_gateway_top1_match_rate": beauty_ranking_summary["gateway_top1_match_rate"],
            "beauty_ranking_pivot_top1_matches": beauty_ranking_summary["pivot_top1_matches"],
            "beauty_ranking_pivot_top1_evaluable": beauty_ranking_summary["pivot_top1_evaluable"],
            "beauty_ranking_pivot_top1_match_rate": beauty_ranking_summary["pivot_top1_match_rate"],
            "beauty_ranking_gateway_nonempty": beauty_ranking_summary["gateway_nonempty"],
            "beauty_ranking_pivot_nonempty": beauty_ranking_summary["pivot_nonempty"],
            "beauty_ranking_raw_seed_available_cases": beauty_ranking_summary["raw_seed_available_cases"],
            "beauty_ranking_compare_present": beauty_ranking_compare_summary["present"],
            "beauty_ranking_top1_match_delta": beauty_ranking_compare_summary["top1_match_delta"],
            "beauty_ranking_improved_query_count": beauty_ranking_compare_summary["improved_query_count"],
            "beauty_ranking_regressed_query_count": beauty_ranking_compare_summary["regressed_query_count"],
            "beauty_ranking_overlap_gain_cases": beauty_ranking_compare_summary["overlap_gain_cases"],
            "beauty_ranking_overlap_loss_cases": beauty_ranking_compare_summary["overlap_loss_cases"],
            "beauty_ranking_non_regressing": beauty_ranking_compare_summary["non_regressing"],
            "commerce_shadow_audit_present": commerce_shadow_summary["present"],
            "commerce_shadow_case_count": commerce_shadow_summary["case_count"],
            "commerce_shadow_top1_matches": commerce_shadow_summary["top1_matches"],
            "commerce_shadow_top1_evaluable": commerce_shadow_summary["top1_evaluable"],
            "commerce_shadow_top1_match_rate": commerce_shadow_summary["top1_match_rate"],
            "commerce_shadow_gateway_nonempty": commerce_shadow_summary["gateway_nonempty"],
            "commerce_shadow_pivot_nonempty": commerce_shadow_summary["pivot_nonempty"],
            "commerce_shadow_no_result_mismatch_cases": commerce_shadow_summary["no_result_mismatch_cases"],
            "commerce_shadow_bad_price_anomaly_cases": commerce_shadow_summary["bad_price_anomaly_cases"],
            "commerce_shadow_compare_present": commerce_shadow_compare_summary["present"],
            "commerce_shadow_top1_match_delta": commerce_shadow_compare_summary["top1_match_delta"],
            "commerce_shadow_improved_query_count": commerce_shadow_compare_summary["improved_query_count"],
            "commerce_shadow_regressed_query_count": commerce_shadow_compare_summary["regressed_query_count"],
            "commerce_shadow_overlap_gain_cases": commerce_shadow_compare_summary["overlap_gain_cases"],
            "commerce_shadow_overlap_loss_cases": commerce_shadow_compare_summary["overlap_loss_cases"],
            "commerce_shadow_non_regressing": commerce_shadow_compare_summary["non_regressing"],
            "semantic_class_summary": {
                "release_gate": release_gate_semantic_class_summary,
                "commerce_shadow": commerce_shadow_summary["semantic_class_summary"],
            },
            "serve_readiness_by_source": serve_readiness_by_source,
        },
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
