#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch runner for multi-merchant commerce channels signoff."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--cohort", required=True, help="Path to cohort JSON manifest.")
    parser.add_argument("--header", action="append", default=[], help="Repeatable raw header in 'Name: Value' form.")
    parser.add_argument("--internal-key", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--sync-wait-seconds", type=float, default=20.0)
    parser.add_argument("--sync-poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--backfill-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    parser.add_argument("--case-id", action="append", default=[], help="Optional repeatable case_id filter.")
    parser.add_argument(
        "--min-enabled-cases",
        type=int,
        default=None,
        help="Override cohort current-environment minimum enabled-case gate for pass/fail evaluation.",
    )
    return parser.parse_args()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_cohort(path_str: str) -> Dict[str, Any]:
    path = Path(path_str)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {
            "cohort_name": path.stem,
            "min_enabled_cases": 1,
            "target_enabled_cases": 1,
            "required_semantic_classes": [],
            "cases": payload,
        }
    if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
        return payload
    raise ValueError(f"Unsupported cohort payload in {path}")


def _slugify(raw: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(raw).strip().lower()).strip("-") or "case"


def _run_case_signoff(case: Dict[str, Any], args: argparse.Namespace, case_output_dir: Optional[Path]) -> Dict[str, Any]:
    case_id = str(case.get("case_id") or case.get("merchant_id") or "case").strip()
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "smoke_commerce_channels_signoff.py"),
        "--base-url",
        args.base_url,
        "--merchant-id",
        str(case["merchant_id"]),
        "--database-url",
        args.database_url,
        "--internal-key",
        args.internal_key,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--sync-wait-seconds",
        str(args.sync_wait_seconds),
        "--sync-poll-interval-seconds",
        str(args.sync_poll_interval_seconds),
        "--backfill-timeout-seconds",
        str(case.get("backfill_timeout_seconds") or args.backfill_timeout_seconds),
    ]
    query = str(case.get("query") or "").strip()
    if query:
        cmd.extend(["--query", query])
    preferred_provider = str(case.get("payment_preferred_provider") or "").strip()
    if preferred_provider:
        cmd.extend(["--payment-preferred-provider", preferred_provider])
    payment_currency = str(case.get("payment_currency") or "").strip()
    if payment_currency:
        cmd.extend(["--payment-currency", payment_currency])
    for raw_header in args.header:
        cmd.extend(["--header", raw_header])
    if case_output_dir is not None:
        case_output_dir.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--output-json", str(case_output_dir / "commerce-channels-signoff.json")])
        cmd.extend(["--output-md", str(case_output_dir / "commerce-channels-signoff.md")])
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = (completed.stdout or "").strip()
    if not stdout:
        payload = {"raw_stderr": (completed.stderr or "")[-4000:]}
    else:
        try:
            parsed = json.loads(stdout)
            payload = parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            payload = {
                "raw_stdout": stdout[-4000:],
                "raw_stderr": (completed.stderr or "")[-4000:],
            }
    return {
        "case_id": case_id,
        "merchant_id": str(case.get("merchant_id") or ""),
        "label": case.get("label"),
        "semantic_class": case.get("semantic_class"),
        "enabled": True,
        "ok": completed.returncode == 0 and bool(payload.get("overall_ok")),
        "returncode": completed.returncode,
        "payload": payload,
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Commerce Channels Signoff Batch",
        "",
        f"- cohort_name: `{report.get('cohort_name')}`",
        f"- overall_ok: `{report.get('overall_ok')}`",
        f"- enabled_cases: `{summary.get('enabled_cases')}`",
        f"- passed_cases: `{summary.get('passed_cases')}`",
        f"- failed_cases: `{summary.get('failed_cases')}`",
        f"- skipped_cases: `{summary.get('skipped_cases')}`",
        f"- min_enabled_cases: `{summary.get('min_enabled_cases')}`",
        f"- meets_min_enabled_cases: `{summary.get('meets_min_enabled_cases')}`",
        f"- target_enabled_cases: `{summary.get('target_enabled_cases')}`",
        f"- meets_target_enabled_cases: `{summary.get('meets_target_enabled_cases')}`",
        "",
        "## Cases",
        "",
    ]
    for case in report.get("cases") or []:
        lines.append(
            f"- `{case['case_id']}` enabled=`{case.get('enabled')}` ok=`{case.get('ok')}` merchant_id=`{case.get('merchant_id')}` semantic_class=`{case.get('semantic_class')}`"
        )
        if case.get("skip_reason"):
            lines.append(f"  skip_reason: `{case['skip_reason']}`")
    if summary.get("missing_semantic_classes"):
        lines.extend(
            [
                "",
                "## Missing Semantic Classes",
                "",
                f"- `{', '.join(summary['missing_semantic_classes'])}`",
            ]
        )
    if summary.get("missing_target_semantic_classes"):
        lines.extend(
            [
                "",
                "## Missing Long-Term Semantic Classes",
                "",
                f"- `{', '.join(summary['missing_target_semantic_classes'])}`",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    cohort = _load_cohort(args.cohort)
    selected_ids = {str(item).strip() for item in (args.case_id or []) if str(item).strip()}
    cases: List[Dict[str, Any]] = []
    output_dir = Path(args.output_dir) if args.output_dir else None

    for raw_case in cohort.get("cases") or []:
        case = dict(raw_case)
        case_id = str(case.get("case_id") or case.get("merchant_id") or "case").strip()
        if selected_ids and case_id not in selected_ids:
            continue
        enabled = bool(case.get("enabled", True))
        if not enabled:
            cases.append(
                {
                    "case_id": case_id,
                    "merchant_id": str(case.get("merchant_id") or ""),
                    "label": case.get("label"),
                    "semantic_class": case.get("semantic_class"),
                    "enabled": False,
                    "ok": None,
                    "skip_reason": case.get("skip_reason") or "disabled",
                }
            )
            continue
        case_dir = output_dir / _slugify(case_id) if output_dir else None
        cases.append(_run_case_signoff(case, args, case_dir))

    enabled_cases = [case for case in cases if case.get("enabled")]
    passed_cases = [case for case in enabled_cases if case.get("ok") is True]
    failed_cases = [case for case in enabled_cases if case.get("ok") is not True]
    skipped_cases = [case for case in cases if not case.get("enabled")]
    required_semantic_classes = [str(item) for item in (cohort.get("required_semantic_classes") or []) if str(item).strip()]
    target_semantic_classes = [str(item) for item in (cohort.get("target_semantic_classes") or []) if str(item).strip()]
    present_semantic_classes = {
        str(case.get("semantic_class"))
        for case in enabled_cases
        if case.get("semantic_class")
    }
    missing_semantic_classes = [item for item in required_semantic_classes if item not in present_semantic_classes]
    missing_target_semantic_classes = [item for item in target_semantic_classes if item not in present_semantic_classes]
    min_enabled_cases = int(args.min_enabled_cases if args.min_enabled_cases is not None else cohort.get("min_enabled_cases") or cohort.get("target_enabled_cases") or 1)
    meets_min_enabled_cases = len(enabled_cases) >= min_enabled_cases
    target_enabled_cases = int(cohort.get("target_enabled_cases") or min_enabled_cases)
    meets_target_enabled_cases = len(enabled_cases) >= target_enabled_cases
    skipped_reason_counts = Counter(str(case.get("skip_reason") or "unknown") for case in skipped_cases)

    summary = {
        "total_cases": len(cases),
        "enabled_cases": len(enabled_cases),
        "passed_cases": len(passed_cases),
        "failed_cases": len(failed_cases),
        "skipped_cases": len(skipped_cases),
        "min_enabled_cases": min_enabled_cases,
        "meets_min_enabled_cases": meets_min_enabled_cases,
        "target_enabled_cases": target_enabled_cases,
        "meets_target_enabled_cases": meets_target_enabled_cases,
        "required_semantic_classes": required_semantic_classes,
        "missing_semantic_classes": missing_semantic_classes,
        "target_semantic_classes": target_semantic_classes,
        "missing_target_semantic_classes": missing_target_semantic_classes,
        "skipped_reason_counts": dict(skipped_reason_counts),
        "semantic_class_summary": dict(
            Counter(str(case.get("semantic_class") or "unspecified") for case in cases)
        ),
    }
    overall_ok = not failed_cases and meets_min_enabled_cases and not missing_semantic_classes
    report = {
        "cohort_name": cohort.get("cohort_name") or Path(args.cohort).stem,
        "overall_ok": overall_ok,
        "summary": summary,
        "cases": cases,
    }
    json_blob = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    markdown = _render_markdown(report)
    _write_if_requested(args.output_json, json_blob + "\n")
    _write_if_requested(args.output_md, markdown)
    print(json_blob)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
