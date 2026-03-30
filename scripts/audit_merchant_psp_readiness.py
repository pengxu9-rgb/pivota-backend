#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.merchant_psp_config_service import (  # noqa: E402
    SUPPORTED_CANONICAL_PSPS,
    evaluate_psp_readiness,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit merchant PSP live-readiness and optionally rerun PSP validation endpoints."
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--merchant-id", action="append", default=[], help="Repeatable merchant_id.")
    parser.add_argument("--merchant-id-file", default=None, help="Optional newline-delimited merchant_id file.")
    parser.add_argument("--base-url", default=None, help="Optional API base URL for live PSP validation.")
    parser.add_argument("--header", action="append", default=[], help="Repeatable raw header in 'Name: Value' form.")
    parser.add_argument("--validate", action="store_true", help="Call /merchant/psp/{psp_id}/test for blocked canonical PSPs.")
    parser.add_argument(
        "--validate-supported-only",
        action="store_true",
        help="When validating, skip unsupported PSPs such as paypal.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _headers(raw_headers: List[str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    for raw in raw_headers:
        if ":" not in str(raw):
            continue
        name, value = raw.split(":", 1)
        if name.strip():
            headers[name.strip()] = value.strip()
    return headers


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _connect_postgres(database_url: str):
    try:
        import psycopg  # type: ignore

        return psycopg.connect(database_url)
    except Exception:
        import psycopg2  # type: ignore

        return psycopg2.connect(database_url)


def _normalize_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    normalized: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _load_merchant_ids(args: argparse.Namespace) -> List[str]:
    merchant_ids: List[str] = []
    for item in args.merchant_id or []:
        merchant_id = str(item or "").strip()
        if merchant_id and merchant_id not in merchant_ids:
            merchant_ids.append(merchant_id)
    if args.merchant_id_file:
        for raw in Path(args.merchant_id_file).read_text(encoding="utf-8").splitlines():
            merchant_id = raw.strip()
            if merchant_id and merchant_id not in merchant_ids:
                merchant_ids.append(merchant_id)
    if not merchant_ids:
        raise SystemExit("at least one --merchant-id or --merchant-id-file is required")
    return merchant_ids


def _fetch_rows(database_url: str, merchant_ids: List[str]) -> List[Dict[str, Any]]:
    sql = """
    SELECT
      merchant_id,
      psp_id,
      provider,
      status,
      api_key,
      account_id,
      provider_config,
      environment,
      validation_status,
      validation_error,
      last_validated_at
    FROM merchant_psps
    WHERE merchant_id = ANY(%s)
    ORDER BY merchant_id, connected_at DESC NULLS LAST, created_at DESC NULLS LAST
    """
    with _connect_postgres(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (merchant_ids,))
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _recommend_actions(readiness: Dict[str, Any]) -> List[str]:
    provider = str(readiness.get("provider") or "").strip().lower()
    blockers = [str(item) for item in (readiness.get("readiness_blockers") or [])]
    actions: List[str] = []

    def add(action: str) -> None:
        if action not in actions:
            actions.append(action)

    for blocker in blockers:
        lowered = blocker.lower()
        if "configured for test" in lowered:
            add(f"replace {provider} test credentials with live credentials")
        elif "validation has not been run" in lowered:
            add(f"run /merchant/psp/{{psp_id}}/test for {provider}")
        elif "validation failed" in lowered:
            add(f"fix {provider} validation error and rerun /merchant/psp/{{psp_id}}/test")
        elif "public key is missing" in lowered:
            add(f"add {provider} public key / provider_config")
        elif "client key is missing" in lowered:
            add(f"add {provider} client key / provider_config")
        elif "processing channel id is missing" in lowered:
            add(f"set {provider} processing_channel_id")
        elif "merchant account is missing" in lowered:
            add(f"set {provider} merchant_account")
        elif "environment is unknown" in lowered:
            add(f"normalize {provider} environment from live credentials and rerun validation")
        elif "connected account does not match" in lowered:
            add("fix stripe connected account / key mismatch")
        elif "webhook endpoint is not configured" in lowered:
            add("provision stripe live webhook endpoint")

    return actions


def _validate_psp(
    *,
    session: requests.Session,
    base_url: str,
    headers: Dict[str, str],
    timeout_seconds: float,
    psp_id: str,
) -> Dict[str, Any]:
    response = session.post(
        f"{base_url.rstrip('/')}/merchant/psp/{psp_id}/test",
        headers=headers,
        timeout=timeout_seconds,
    )
    try:
        body = response.json()
    except Exception:
        body = {"raw_text": response.text[:2000]}
    return {
        "http_status": response.status_code,
        "body": body,
    }


def _build_merchant_report(
    merchant_id: str,
    rows: List[Dict[str, Any]],
    *,
    validation_results: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    psps: List[Dict[str, Any]] = []
    supported_active: List[str] = []
    live_ready_supported: List[str] = []
    provider_blocker_counts: Counter[str] = Counter()

    for row in rows:
        provider = str(row.get("provider") or "").strip().lower()
        readiness = evaluate_psp_readiness(
            provider,
            status=row.get("status"),
            api_key=row.get("api_key"),
            account_id=row.get("account_id"),
            provider_config=row.get("provider_config"),
            environment=row.get("environment"),
            validation_status=row.get("validation_status"),
            validation_error=row.get("validation_error"),
        )
        psp_record = {
            "psp_id": row.get("psp_id"),
            "provider": provider,
            "status": row.get("status"),
            "environment": readiness.get("environment"),
            "validation_status": readiness.get("validation_status"),
            "live_charge_ready": readiness.get("live_charge_ready"),
            "readiness_blockers": readiness.get("readiness_blockers") or [],
            "recommended_actions": _recommend_actions(readiness),
            "last_validated_at": str(row.get("last_validated_at") or "") or None,
        }
        if validation_results and row.get("psp_id") in validation_results:
            psp_record["validation_attempt"] = validation_results[str(row.get("psp_id"))]
        psps.append(psp_record)

        if str(row.get("status") or "").strip().lower() in {"active", "connected", "validated"}:
            if provider in SUPPORTED_CANONICAL_PSPS and provider not in supported_active:
                supported_active.append(provider)
            if provider in SUPPORTED_CANONICAL_PSPS and readiness.get("live_charge_ready") and provider not in live_ready_supported:
                live_ready_supported.append(provider)
            for blocker in readiness.get("readiness_blockers") or []:
                provider_blocker_counts[f"{provider}:{blocker}"] += 1

    blocking_supported = [
        {
            "provider": item["provider"],
            "psp_id": item["psp_id"],
            "blockers": item["readiness_blockers"],
            "recommended_actions": item["recommended_actions"],
        }
        for item in psps
        if item["provider"] in SUPPORTED_CANONICAL_PSPS and not item["live_charge_ready"]
    ]

    return {
        "merchant_id": merchant_id,
        "psp_count": len(psps),
        "supported_active_psp_providers": supported_active,
        "live_ready_supported_psp_providers": live_ready_supported,
        "ready_for_order_backed_canary": bool(live_ready_supported),
        "psps": psps,
        "blocking_supported_psps": blocking_supported,
        "provider_blocker_counts": dict(sorted(provider_blocker_counts.items())),
    }


def _build_report(
    rows: List[Dict[str, Any]],
    merchant_ids: List[str],
    *,
    validation_results: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    rows_by_merchant: Dict[str, List[Dict[str, Any]]] = {merchant_id: [] for merchant_id in merchant_ids}
    for row in rows:
        merchant_id = str(row.get("merchant_id") or "").strip()
        rows_by_merchant.setdefault(merchant_id, []).append(row)

    merchants = [
        _build_merchant_report(merchant_id, rows_by_merchant.get(merchant_id, []), validation_results=validation_results)
        for merchant_id in merchant_ids
    ]
    ready_merchants = [item for item in merchants if item.get("ready_for_order_backed_canary")]
    blocker_counts = Counter()
    for merchant in merchants:
        for psp in merchant.get("blocking_supported_psps") or []:
            for blocker in psp.get("blockers") or []:
                blocker_counts[str(blocker)] += 1

    summary = {
        "merchant_count": len(merchants),
        "ready_merchants": len(ready_merchants),
        "blocked_merchants": len(merchants) - len(ready_merchants),
        "merchants_with_supported_active_psp": sum(
            1 for item in merchants if bool(item.get("supported_active_psp_providers"))
        ),
        "merchants_with_live_ready_supported_psp": len(ready_merchants),
        "blocker_counts": dict(sorted(blocker_counts.items())),
    }
    return {
        "overall_ok": summary["blocked_merchants"] == 0,
        "summary": summary,
        "merchants": merchants,
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Merchant PSP Readiness Audit",
        "",
        f"- merchant_count: `{summary.get('merchant_count')}`",
        f"- ready_merchants: `{summary.get('ready_merchants')}`",
        f"- blocked_merchants: `{summary.get('blocked_merchants')}`",
        f"- merchants_with_supported_active_psp: `{summary.get('merchants_with_supported_active_psp')}`",
        f"- merchants_with_live_ready_supported_psp: `{summary.get('merchants_with_live_ready_supported_psp')}`",
        "",
        "## Merchants",
        "",
    ]
    for merchant in report.get("merchants") or []:
        lines.append(
            f"- `{merchant['merchant_id']}` ready_for_order_backed_canary=`{merchant.get('ready_for_order_backed_canary')}` live_ready_supported_psp_providers=`{', '.join(merchant.get('live_ready_supported_psp_providers') or []) or 'n/a'}`"
        )
        for blocked in merchant.get("blocking_supported_psps") or []:
            blockers = ", ".join(blocked.get("blockers") or []) or "n/a"
            actions = ", ".join(blocked.get("recommended_actions") or []) or "n/a"
            lines.append(
                f"  - `{blocked['provider']}` `{blocked['psp_id']}` blockers=`{blockers}` actions=`{actions}`"
            )
    if summary.get("blocker_counts"):
        lines.extend(["", "## Blocker Counts", ""])
        for blocker, count in (summary.get("blocker_counts") or {}).items():
            lines.append(f"- `{blocker}`: `{count}`")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    merchant_ids = _load_merchant_ids(args)
    rows = _fetch_rows(args.database_url, merchant_ids)

    validation_results: Dict[str, Dict[str, Any]] = {}
    if args.validate:
        if not args.base_url:
            raise SystemExit("--base-url is required when --validate is set")
        headers = _headers(args.header)
        session = requests.Session()
        for row in rows:
            provider = str(row.get("provider") or "").strip().lower()
            psp_id = str(row.get("psp_id") or "").strip()
            if not psp_id:
                continue
            if args.validate_supported_only and provider not in SUPPORTED_CANONICAL_PSPS:
                continue
            readiness = evaluate_psp_readiness(
                provider,
                status=row.get("status"),
                api_key=row.get("api_key"),
                account_id=row.get("account_id"),
                provider_config=row.get("provider_config"),
                environment=row.get("environment"),
                validation_status=row.get("validation_status"),
                validation_error=row.get("validation_error"),
            )
            if readiness.get("live_charge_ready"):
                continue
            validation_results[psp_id] = _validate_psp(
                session=session,
                base_url=args.base_url,
                headers=headers,
                timeout_seconds=args.timeout_seconds,
                psp_id=psp_id,
            )

    report = _build_report(rows, merchant_ids, validation_results=validation_results)
    payload = json.dumps(report, ensure_ascii=True, indent=2)
    markdown = _render_markdown(report)
    _write_if_requested(args.output_json, payload + "\n")
    _write_if_requested(args.output_md, markdown)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
