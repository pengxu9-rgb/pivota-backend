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


def main() -> int:
    args = _parse_args()
    artifacts = {
        "migration": _load_optional_json(args.migration),
        "backfill_verify": _load_optional_json(args.backfill_verify_json),
        "release_gate": _load_optional_json(args.release_gate_json),
        "catalog_pivot_smoke": _load_optional_json(args.catalog_pivot_smoke_json),
        "search_chain_probe": _load_optional_json(args.search_chain_probe_json),
    }

    release_gate_summary = ((artifacts.get("release_gate") or {}).get("summary") or {}) if isinstance(artifacts.get("release_gate"), dict) else {}
    smoke_ok = bool((artifacts.get("catalog_pivot_smoke") or {}).get("overall_ok")) if isinstance(artifacts.get("catalog_pivot_smoke"), dict) else False
    backfill_summary = ((artifacts.get("backfill_verify") or {}).get("summary") or {}) if isinstance(artifacts.get("backfill_verify"), dict) else {}
    search_chain_summary = _summarize_search_chain_probe(artifacts.get("search_chain_probe"))
    release_gate_failed_cases = release_gate_summary.get("failed_cases")
    blocking_ready = (release_gate_failed_cases in (0, None)) and smoke_ok

    report = {
        "label": args.label,
        "artifacts": artifacts,
        "summary": {
            "blocking_ready": blocking_ready,
            "release_gate_failed_cases": release_gate_failed_cases,
            "release_gate_passed_cases": release_gate_summary.get("passed_cases"),
            "catalog_pivot_smoke_ok": smoke_ok,
            "backfill_missing_product_keys_count": ((backfill_summary.get("verify") or {}).get("missing_product_keys_count") if isinstance(backfill_summary.get("verify"), dict) else None),
            "search_chain_probe_present": search_chain_summary["present"],
            "search_chain_probe_records_total": search_chain_summary["records_total"],
            "search_chain_probe_records_ok": search_chain_summary["records_ok"],
            "search_chain_probe_records_http_200": search_chain_summary["records_http_200"],
            "search_chain_probe_records_nonempty": search_chain_summary["records_nonempty"],
            "search_chain_probe_legacy_parity_ok": search_chain_summary["legacy_parity_ok"],
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
