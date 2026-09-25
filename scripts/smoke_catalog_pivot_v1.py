#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live smoke for /v1/catalog/* and /v1/pivot/* endpoints."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--query", default="vitamin c serum")
    parser.add_argument("--offer-id", default=None)
    parser.add_argument("--product-key", default=None)
    parser.add_argument("--sku-key", default=None)
    parser.add_argument("--skip-pivot-query", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--header", action="append", default=[], help=(
            "Repeatable raw header in 'Name: Value' form. The /v1/catalog/* steps "
            "need an admin/super_admin JWT: those routes are tenant-scoped, so a "
            "non-admin token may only name the merchant in its own merchant_id "
            "claim and otherwise gets 403 'cannot run catalog jobs for another "
            "merchant'. Mint one with scripts/mint_employee_jwt.py --role admin."
        ))
    parser.add_argument("--catalog-migration-verify-smoke", action="store_true")
    parser.add_argument("--catalog-migration-run-smoke", action="store_true")
    parser.add_argument(
        "--catalog-migration-run-mode",
        choices=("apply", "apply-verify"),
        default="apply-verify",
    )
    parser.add_argument("--catalog-webhook-smoke", action="store_true")
    parser.add_argument("--catalog-sync-job-smoke", action="store_true")
    parser.add_argument("--catalog-sync-limit", type=int, default=1)
    parser.add_argument("--catalog-sync-wait-seconds", type=float, default=0.0)
    parser.add_argument("--catalog-sync-poll-interval-seconds", type=float, default=2.0)
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


def _write(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _record_step(
    *,
    steps: List[Dict[str, Any]],
    name: str,
    response: requests.Response,
    started: float,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    try:
        body = response.json()
    except Exception:
        body = {"raw_text": response.text[:1000]}
    record = {
        "step": name,
        "status_code": response.status_code,
        "elapsed_ms": elapsed_ms,
        "ok": 200 <= response.status_code < 300,
        "body": body,
    }
    if extra:
        record.update(extra)
    steps.append(record)
    return record


def _record_manual_step(
    *,
    steps: List[Dict[str, Any]],
    name: str,
    ok: bool,
    elapsed_ms: float,
    body: Dict[str, Any],
    status_code: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record = {
        "step": name,
        "status_code": status_code,
        "elapsed_ms": round(float(elapsed_ms), 1),
        "ok": bool(ok),
        "body": body,
    }
    if extra:
        record.update(extra)
    steps.append(record)
    return record


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Catalog Pivot V1 Smoke Report",
        "",
        f"- base_url: `{report['base_url']}`",
        f"- merchant_id: `{report['merchant_id']}`",
        f"- overall_ok: `{report['overall_ok']}`",
        "",
        "## Steps",
        "",
    ]
    for step in report["steps"]:
        lines.append(
            f"- `{step['step']}` status=`{step['status_code']}` elapsed_ms=`{step['elapsed_ms']}` ok=`{step['ok']}`"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    session = requests.Session()
    headers = _headers(args.header)
    base_url = args.base_url.rstrip("/")
    steps: List[Dict[str, Any]] = []

    first_item: Dict[str, Any] = {}
    if not args.skip_pivot_query:
        started = time.perf_counter()
        pivot_query_resp = session.post(
            f"{base_url}/v1/pivot/query",
            headers=headers,
            json={
                "query": args.query,
                "merchant_id": args.merchant_id,
                "limit": 5,
                "include_external": True,
                "include_incentives": True,
            },
            timeout=args.timeout_seconds,
        )
        pivot_query = _record_step(steps=steps, name="pivot_query", response=pivot_query_resp, started=started)

        query_body = pivot_query.get("body") if isinstance(pivot_query, dict) else {}
        first_item = ((query_body or {}).get("items") or [{}])[0] if isinstance((query_body or {}).get("items"), list) else {}
    derived_product_key = args.product_key or ((first_item.get("product") or {}).get("product_key"))
    derived_sku_key = args.sku_key or ((first_item.get("sku") or {}).get("sku_key"))
    derived_offer_id = args.offer_id
    if not derived_offer_id:
        offers = (first_item.get("offers") or []) if isinstance(first_item, dict) else []
        if isinstance(offers, list) and offers:
            derived_offer_id = (offers[0] or {}).get("offer_id")

    started = time.perf_counter()
    offers_resp = session.post(
        f"{base_url}/v1/pivot/offers/resolve",
        headers=headers,
        json={
            "merchant_id": args.merchant_id,
            "product_key": derived_product_key,
            "sku_key": derived_sku_key,
            "query": args.query,
            "include_external": True,
        },
        timeout=args.timeout_seconds,
    )
    _record_step(steps=steps, name="pivot_offers_resolve", response=offers_resp, started=started)

    quote_payload: Dict[str, Any] = {
        "merchant_id": args.merchant_id,
        "items": [],
    }
    if derived_offer_id:
        quote_payload["items"].append({"offer_id": derived_offer_id, "quantity": 1})
    elif derived_sku_key:
        quote_payload["items"].append({"sku_key": derived_sku_key, "quantity": 1})
    elif derived_product_key:
        quote_payload["items"].append({"product_key": derived_product_key, "quantity": 1})

    started = time.perf_counter()
    quote_resp = session.post(
        f"{base_url}/v1/pivot/quote",
        headers=headers,
        json=quote_payload,
        timeout=args.timeout_seconds,
    )
    _record_step(steps=steps, name="pivot_quote", response=quote_resp, started=started)

    if args.catalog_migration_run_smoke:
        started = time.perf_counter()
        migration_run_resp = session.post(
            f"{base_url}/admin/migrations/run/058",
            headers=headers,
            json={"mode": args.catalog_migration_run_mode},
            timeout=args.timeout_seconds,
        )
        _record_step(
            steps=steps,
            name="admin_catalog_migration_run_058",
            response=migration_run_resp,
            started=started,
            extra={"mode": args.catalog_migration_run_mode},
        )

    if args.catalog_migration_verify_smoke:
        started = time.perf_counter()
        migration_verify_resp = session.get(
            f"{base_url}/admin/migrations/verify/058",
            headers=headers,
            timeout=args.timeout_seconds,
        )
        _record_step(
            steps=steps,
            name="admin_catalog_migration_verify_058",
            response=migration_verify_resp,
            started=started,
        )

        started = time.perf_counter()
        migration_verify_059_resp = session.get(
            f"{base_url}/admin/migrations/verify/059",
            headers=headers,
            timeout=args.timeout_seconds,
        )
        _record_step(
            steps=steps,
            name="admin_catalog_migration_verify_059",
            response=migration_verify_059_resp,
            started=started,
        )

    if args.catalog_webhook_smoke:
        started = time.perf_counter()
        webhook_resp = session.post(
            f"{base_url}/v1/catalog/connectors/shopify/webhooks",
            headers=headers,
            params={
                "merchant_id": args.merchant_id,
                "event_type": "pivot_smoke_check",
                "topic": "products/update",
                "source_ref": f"pivot_smoke_{int(time.time())}",
            },
            json={"smoke": True, "query": args.query},
            timeout=args.timeout_seconds,
        )
        _record_step(steps=steps, name="catalog_webhook_ingest", response=webhook_resp, started=started)

    if args.catalog_sync_job_smoke:
        started = time.perf_counter()
        create_job_resp = session.post(
            f"{base_url}/v1/catalog/sync/jobs",
            headers=headers,
            json={
                "merchant_id": args.merchant_id,
                "connector": "shopify",
                "mode": "reconcile",
                "platform": "shopify",
                "force_refresh": False,
                "limit": args.catalog_sync_limit,
                "sync_from_cache": True,
            },
            timeout=args.timeout_seconds,
        )
        create_step = _record_step(steps=steps, name="catalog_sync_job_create", response=create_job_resp, started=started)
        job_id = ((create_step.get("body") or {}).get("job_id") if isinstance(create_step, dict) else None)
        if job_id:
            if args.catalog_sync_wait_seconds > 0:
                deadline = time.perf_counter() + args.catalog_sync_wait_seconds
                while True:
                    started = time.perf_counter()
                    read_job_resp = session.get(
                        f"{base_url}/v1/catalog/sync/jobs/{job_id}",
                        headers=headers,
                        timeout=args.timeout_seconds,
                    )
                    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    try:
                        read_job_body = read_job_resp.json()
                    except Exception:
                        read_job_body = {"raw_text": read_job_resp.text[:1000]}
                    status = read_job_body.get("status") if isinstance(read_job_body, dict) else None
                    if status in {"completed", "failed"}:
                        _record_manual_step(
                            steps=steps,
                            name="catalog_sync_job_final",
                            ok=(200 <= read_job_resp.status_code < 300) and status == "completed",
                            elapsed_ms=elapsed_ms,
                            body=read_job_body,
                            status_code=read_job_resp.status_code,
                            extra={"job_id": job_id},
                        )
                        break
                    if time.perf_counter() >= deadline:
                        _record_manual_step(
                            steps=steps,
                            name="catalog_sync_job_final",
                            ok=False,
                            elapsed_ms=elapsed_ms,
                            body={"status": "timeout", "job_id": job_id},
                            status_code=read_job_resp.status_code,
                            extra={"job_id": job_id},
                        )
                        break
                    time.sleep(max(args.catalog_sync_poll_interval_seconds, 0.1))
            else:
                started = time.perf_counter()
                read_job_resp = session.get(
                    f"{base_url}/v1/catalog/sync/jobs/{job_id}",
                    headers=headers,
                    timeout=args.timeout_seconds,
                )
                _record_step(
                    steps=steps,
                    name="catalog_sync_job_read",
                    response=read_job_resp,
                    started=started,
                    extra={"job_id": job_id},
                )

    report = {
        "base_url": base_url,
        "merchant_id": args.merchant_id,
        "query": args.query,
        "overall_ok": all(step.get("ok") for step in steps),
        "steps": steps,
    }
    json_blob = json.dumps(report, indent=2, ensure_ascii=False)
    _write(args.output_json, json_blob + "\n")
    _write(args.output_md, _render_markdown(report))
    print(json_blob)
    return 0 if report["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
