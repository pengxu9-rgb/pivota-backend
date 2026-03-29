#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Celestial Pivot release bundle end-to-end and collect evidence artifacts."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--release-gate-base-url", default=None)
    parser.add_argument("--smoke-base-url", default=None)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--merchant-id", action="append", dest="merchant_ids", required=True)
    parser.add_argument("--smoke-merchant-id", default=None)
    parser.add_argument("--label", default="celestial-pivot-production-ready")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--migration-artifact", default=None)
    parser.add_argument(
        "--migration-mode",
        choices=("skip", "verify", "apply", "apply-verify"),
        default="skip",
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or "")
    parser.add_argument("--backfill-platform", default=None)
    parser.add_argument("--backfill-limit", type=int, default=10)
    parser.add_argument("--backfill-include-expired", action="store_true")
    parser.add_argument("--smoke-query", default="vitamin c serum")
    parser.add_argument("--smoke-offer-id", default=None)
    parser.add_argument("--smoke-product-key", default=None)
    parser.add_argument("--smoke-sku-key", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--catalog-migration-verify-smoke", action="store_true")
    parser.add_argument("--catalog-webhook-smoke", action="store_true")
    parser.add_argument("--catalog-sync-job-smoke", action="store_true")
    parser.add_argument("--catalog-sync-limit", type=int, default=1)
    parser.add_argument("--catalog-sync-wait-seconds", type=float, default=0.0)
    parser.add_argument("--catalog-sync-poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--service-side-data-plane-verify", action="store_true")
    parser.add_argument("--search-chain-probe", action="store_true")
    parser.add_argument("--search-chain-probe-blocking", action="store_true")
    parser.add_argument("--probe-agent-base-url", default=None)
    parser.add_argument("--probe-gateway-url", default=None)
    parser.add_argument("--probe-source", default="shopping_agent")
    parser.add_argument("--probe-rounds", type=int, default=3)
    parser.add_argument("--probe-limit", type=int, default=24)
    parser.add_argument("--probe-sleep-ms", type=int, default=250)
    parser.add_argument("--probe-queries", nargs="*", default=None)
    parser.add_argument("--probe-agent-api-key", default=os.getenv("AGENT_API_KEY") or "")
    parser.add_argument(
        "--probe-gateway-api-key",
        default=(os.getenv("GATEWAY_API_KEY") or os.getenv("X_API_KEY") or os.getenv("API_KEY") or ""),
    )
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--smoke-header", action="append", default=[])
    parser.add_argument(
        "--release-gate-default-rollout-mode",
        choices=("shadow", "serve", "legacy"),
        default="shadow",
    )
    parser.add_argument("--skip-backfill-apply", action="store_true")
    parser.add_argument("--skip-backfill-verify", action="store_true")
    parser.add_argument("--skip-release-gate", action="store_true")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--skip-evidence", action="store_true")
    return parser.parse_args()


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_json(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if not path or not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload.setdefault("_path", str(path))
        return payload
    return {"raw": payload, "_path": str(path)}


def _render_bundle_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Celestial Pivot Release Bundle",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- label: `{report['label']}`",
        f"- base_url: `{report['base_url']}`",
        f"- overall_ok: `{report['overall_ok']}`",
        "",
        "## Steps",
        "",
    ]
    for step in report["steps"]:
        lines.append(
            f"- `{step['name']}` ok=`{step['ok']}` blocking=`{step['blocking']}` returncode=`{step['returncode']}`"
        )
    lines.extend(["", "## Outputs", ""])
    for key, value in sorted((report.get("outputs") or {}).items()):
        lines.append(f"- {key}: `{value or 'not_provided'}`")
    return "\n".join(lines) + "\n"


def _render_combined_backfill_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Catalog Backfill Verify Bundle",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- merchants_total: `{report['summary']['merchants_total']}`",
        f"- verify_reports_total: `{report['summary']['verify_reports_total']}`",
        f"- total_missing_product_keys_count: `{report['summary']['total_missing_product_keys_count']}`",
        "",
        "## Merchants",
        "",
    ]
    for item in report["reports"]:
        merchant_id = item.get("merchant_id")
        missing = ((item.get("summary") or {}).get("verify") or {}).get("missing_product_keys_count")
        lines.append(f"- `{merchant_id}` missing_product_keys_count=`{missing}`")
    return "\n".join(lines) + "\n"


def _run_python_script(script_path: Path, script_args: Sequence[str]) -> Dict[str, Any]:
    cmd = [sys.executable, str(script_path), *script_args]
    started = time.perf_counter()
    env = dict(os.environ)
    repo_root = str(SCRIPT_DIR.parent)
    existing_pythonpath = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = (
        repo_root
        if not existing_pythonpath
        else os.pathsep.join([repo_root, existing_pythonpath])
    )
    completed = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        cwd=str(SCRIPT_DIR.parent),
        env=env,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    return {
        "cmd": cmd,
        "returncode": int(completed.returncode),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "elapsed_ms": elapsed_ms,
    }


def _header_args(headers: Sequence[str]) -> List[str]:
    args: List[str] = []
    for header in headers:
        args.extend(["--header", header])
    return args


def _record_step(
    *,
    steps: List[Dict[str, Any]],
    name: str,
    result: Dict[str, Any],
    output_json: Optional[Path] = None,
    output_md: Optional[Path] = None,
) -> Dict[str, Any]:
    record = {
        "name": name,
        "ok": int(result.get("returncode") or 0) == 0,
        "blocking": True,
        "returncode": int(result.get("returncode") or 0),
        "elapsed_ms": result.get("elapsed_ms"),
        "cmd": result.get("cmd"),
        "stdout_preview": str(result.get("stdout") or "")[:1000],
        "stderr_preview": str(result.get("stderr") or "")[:1000],
        "output_json": str(output_json) if output_json else None,
        "output_md": str(output_md) if output_md else None,
    }
    steps.append(record)
    return record


def _aggregate_backfill_verify(
    *,
    reports: List[Dict[str, Any]],
    output_json: Path,
    output_md: Path,
) -> Dict[str, Any]:
    total_missing = 0
    catalog_products = 0
    catalog_skus = 0
    catalog_offers = 0
    for report in reports:
        summary = report.get("summary") or {}
        verify = summary.get("verify") or {}
        total_missing += int(verify.get("missing_product_keys_count") or 0)
        catalog_products += int(verify.get("catalog_products") or 0)
        catalog_skus += int(verify.get("catalog_skus") or 0)
        catalog_offers += int(verify.get("catalog_offers") or 0)

    combined = {
        "generated_at": _utc_timestamp(),
        "reports": reports,
        "summary": {
            "merchants_total": len({str(item.get('merchant_id') or '') for item in reports}),
            "verify_reports_total": len(reports),
            "total_missing_product_keys_count": total_missing,
            "catalog_products_total": catalog_products,
            "catalog_skus_total": catalog_skus,
            "catalog_offers_total": catalog_offers,
        },
    }
    _write(output_json, json.dumps(combined, indent=2, ensure_ascii=False) + "\n")
    _write(output_md, _render_combined_backfill_markdown(combined))
    return combined


def _run_bundle(
    args: argparse.Namespace,
    *,
    runner=_run_python_script,
) -> Tuple[Dict[str, Any], int]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    backfill_dir = output_dir / "backfill"
    steps: List[Dict[str, Any]] = []
    outputs: Dict[str, Optional[str]] = {
        "migration_artifact": args.migration_artifact,
        "data_plane_validation_mode": "service-side" if args.service_side_data_plane_verify else "local",
    }
    verify_reports: List[Dict[str, Any]] = []

    if args.migration_mode != "skip" and not args.service_side_data_plane_verify:
        migration_json = output_dir / "catalog-migration-058.json"
        migration_md = output_dir / "catalog-migration-058.md"
        command = [
            "--mode",
            args.migration_mode,
            "--output-json",
            str(migration_json),
            "--output-md",
            str(migration_md),
        ]
        if args.database_url:
            command.extend(["--database-url", args.database_url])
        result = runner(SCRIPT_DIR / "catalog_migration_058.py", command)
        _record_step(
            steps=steps,
            name="catalog_migration_058",
            result=result,
            output_json=migration_json,
            output_md=migration_md,
        )
        outputs["migration_artifact"] = str(migration_json)

    for merchant_id in args.merchant_ids:
        if not args.skip_backfill_apply and not args.service_side_data_plane_verify:
            apply_json = backfill_dir / f"{merchant_id}-apply.json"
            apply_md = backfill_dir / f"{merchant_id}-apply.md"
            command = [
                "--merchant-id",
                merchant_id,
                "--mode",
                "apply",
                "--limit",
                str(args.backfill_limit),
                "--output-json",
                str(apply_json),
                "--output-md",
                str(apply_md),
            ]
            if args.backfill_platform:
                command.extend(["--platform", args.backfill_platform])
            if args.backfill_include_expired:
                command.append("--include-expired")
            result = runner(SCRIPT_DIR / "catalog_backfill_verify.py", command)
            _record_step(
                steps=steps,
                name=f"catalog_backfill_apply:{merchant_id}",
                result=result,
                output_json=apply_json,
                output_md=apply_md,
            )

        if not args.skip_backfill_verify and not args.service_side_data_plane_verify:
            verify_json = backfill_dir / f"{merchant_id}-verify.json"
            verify_md = backfill_dir / f"{merchant_id}-verify.md"
            command = [
                "--merchant-id",
                merchant_id,
                "--mode",
                "verify",
                "--limit",
                str(args.backfill_limit),
                "--output-json",
                str(verify_json),
                "--output-md",
                str(verify_md),
            ]
            if args.backfill_platform:
                command.extend(["--platform", args.backfill_platform])
            if args.backfill_include_expired:
                command.append("--include-expired")
            result = runner(SCRIPT_DIR / "catalog_backfill_verify.py", command)
            _record_step(
                steps=steps,
                name=f"catalog_backfill_verify:{merchant_id}",
                result=result,
                output_json=verify_json,
                output_md=verify_md,
            )
            payload = _load_json(verify_json)
            if payload:
                verify_reports.append(payload)

    combined_backfill_json: Optional[Path] = None
    combined_backfill_md: Optional[Path] = None
    if verify_reports:
        combined_backfill_json = output_dir / "catalog-backfill-verify-bundle.json"
        combined_backfill_md = output_dir / "catalog-backfill-verify-bundle.md"
        _aggregate_backfill_verify(
            reports=verify_reports,
            output_json=combined_backfill_json,
            output_md=combined_backfill_md,
        )
        outputs["backfill_verify_json"] = str(combined_backfill_json)
        outputs["backfill_verify_md"] = str(combined_backfill_md)

    release_gate_json: Optional[Path] = None
    release_gate_md: Optional[Path] = None
    if not args.skip_release_gate:
        release_gate_json = output_dir / "pivot-release-gate.json"
        release_gate_md = output_dir / "pivot-release-gate.md"
        release_gate_base_url = args.release_gate_base_url or args.base_url
        command = [
            "--base-url",
            release_gate_base_url,
            "--corpus",
            args.corpus,
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--default-rollout-mode",
            args.release_gate_default_rollout_mode,
            "--output-json",
            str(release_gate_json),
            "--output-md",
            str(release_gate_md),
            *_header_args(args.header),
        ]
        result = runner(SCRIPT_DIR / "pivot_multi_release_gate.py", command)
        _record_step(
            steps=steps,
            name="pivot_multi_release_gate",
            result=result,
            output_json=release_gate_json,
            output_md=release_gate_md,
        )
        outputs["release_gate_json"] = str(release_gate_json)
        outputs["release_gate_md"] = str(release_gate_md)

    smoke_json: Optional[Path] = None
    smoke_md: Optional[Path] = None
    if not args.skip_smoke:
        smoke_merchant = args.smoke_merchant_id or args.merchant_ids[0]
        smoke_json = output_dir / "catalog-pivot-smoke.json"
        smoke_md = output_dir / "catalog-pivot-smoke.md"
        smoke_base_url = args.smoke_base_url or args.base_url
        smoke_headers = [*list(args.header), *list(args.smoke_header)]
        command = [
            "--base-url",
            smoke_base_url,
            "--merchant-id",
            smoke_merchant,
            "--query",
            args.smoke_query,
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--catalog-sync-limit",
            str(args.catalog_sync_limit),
            "--catalog-sync-wait-seconds",
            str(args.catalog_sync_wait_seconds),
            "--catalog-sync-poll-interval-seconds",
            str(args.catalog_sync_poll_interval_seconds),
            "--output-json",
            str(smoke_json),
            "--output-md",
            str(smoke_md),
            *_header_args(smoke_headers),
        ]
        if args.smoke_offer_id:
            command.extend(["--offer-id", args.smoke_offer_id])
        if args.smoke_product_key:
            command.extend(["--product-key", args.smoke_product_key])
        if args.smoke_sku_key:
            command.extend(["--sku-key", args.smoke_sku_key])
        if args.catalog_migration_verify_smoke:
            command.append("--catalog-migration-verify-smoke")
        if args.service_side_data_plane_verify:
            command.append("--skip-pivot-query")
            command.append("--catalog-migration-verify-smoke")
            command.append("--catalog-sync-job-smoke")
        if args.catalog_webhook_smoke:
            command.append("--catalog-webhook-smoke")
        if args.catalog_sync_job_smoke:
            command.append("--catalog-sync-job-smoke")
        result = runner(SCRIPT_DIR / "smoke_catalog_pivot_v1.py", command)
        _record_step(
            steps=steps,
            name="smoke_catalog_pivot_v1",
            result=result,
            output_json=smoke_json,
            output_md=smoke_md,
        )
        outputs["catalog_pivot_smoke_json"] = str(smoke_json)
        outputs["catalog_pivot_smoke_md"] = str(smoke_md)

    probe_json: Optional[Path] = None
    probe_md: Optional[Path] = None
    if args.search_chain_probe:
        probe_json = output_dir / "search-chain-inventory-probe.json"
        probe_md = output_dir / "search-chain-inventory-probe.md"
        probe_agent_base_url = args.probe_agent_base_url or args.base_url
        probe_gateway_url = args.probe_gateway_url or f"{args.base_url.rstrip('/')}/api/gateway"
        command = [
            "--rounds",
            str(args.probe_rounds),
            "--limit",
            str(args.probe_limit),
            "--sleep-ms",
            str(args.probe_sleep_ms),
            "--agent-base-url",
            probe_agent_base_url,
            "--gateway-url",
            probe_gateway_url,
            "--source",
            args.probe_source,
            "--output-json",
            str(probe_json),
            "--output-md",
            str(probe_md),
        ]
        if args.probe_queries:
            command.extend(["--queries", *args.probe_queries])
        if args.probe_agent_api_key:
            command.extend(["--agent-api-key", args.probe_agent_api_key])
        if args.probe_gateway_api_key:
            command.extend(["--gateway-api-key", args.probe_gateway_api_key])
        result = runner(SCRIPT_DIR / "search_chain_inventory_probe.py", command)
        _record_step(
            steps=steps,
            name="search_chain_inventory_probe",
            result=result,
            output_json=probe_json,
            output_md=probe_md,
        )["blocking"] = bool(args.search_chain_probe_blocking)
        outputs["search_chain_probe_json"] = str(probe_json)
        outputs["search_chain_probe_md"] = str(probe_md)

    evidence_json: Optional[Path] = None
    evidence_md: Optional[Path] = None
    if not args.skip_evidence:
        evidence_json = output_dir / "pivot-release-evidence.json"
        evidence_md = output_dir / "pivot-release-evidence.md"
        command = [
            "--label",
            args.label,
            "--output-json",
            str(evidence_json),
            "--output-md",
            str(evidence_md),
        ]
        migration_artifact = outputs.get("migration_artifact") or args.migration_artifact
        if migration_artifact:
            command.extend(["--migration", migration_artifact])
        if combined_backfill_json:
            command.extend(["--backfill-verify-json", str(combined_backfill_json)])
        if release_gate_json:
            command.extend(["--release-gate-json", str(release_gate_json)])
        if smoke_json:
            command.extend(["--catalog-pivot-smoke-json", str(smoke_json)])
        if probe_json:
            command.extend(["--search-chain-probe-json", str(probe_json)])
        result = runner(SCRIPT_DIR / "build_pivot_release_evidence.py", command)
        _record_step(
            steps=steps,
            name="build_pivot_release_evidence",
            result=result,
            output_json=evidence_json,
            output_md=evidence_md,
        )
        outputs["evidence_json"] = str(evidence_json)
        outputs["evidence_md"] = str(evidence_md)

    blocking_steps = [step for step in steps if step.get("blocking", True)]
    overall_ok = all(step.get("ok") for step in blocking_steps)
    report = {
        "generated_at": _utc_timestamp(),
        "label": args.label,
        "base_url": args.base_url,
        "release_gate_base_url": args.release_gate_base_url or args.base_url,
        "smoke_base_url": args.smoke_base_url or args.base_url,
        "merchant_ids": list(args.merchant_ids),
        "smoke_merchant_id": args.smoke_merchant_id or args.merchant_ids[0],
        "overall_ok": overall_ok,
        "blocking_steps_total": len(blocking_steps),
        "non_blocking_steps_total": max(0, len(steps) - len(blocking_steps)),
        "steps": steps,
        "outputs": outputs,
    }
    summary_json = output_dir / "pivot-release-bundle-summary.json"
    summary_md = output_dir / "pivot-release-bundle-summary.md"
    _write(summary_json, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    _write(summary_md, _render_bundle_markdown(report))
    outputs["bundle_summary_json"] = str(summary_json)
    outputs["bundle_summary_md"] = str(summary_md)
    return report, (0 if overall_ok else 1)


def main() -> int:
    args = _parse_args()
    report, exit_code = _run_bundle(args)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
