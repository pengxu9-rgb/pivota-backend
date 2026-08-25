#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Production-safe signoff for catalog read/write and payment order-backed canary channels."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--query", default=None)
    parser.add_argument("--header", action="append", default=[], help=(
            "Repeatable raw header in 'Name: Value' form. The /v1/catalog/* steps "
            "need an admin/super_admin JWT: those routes are tenant-scoped, so a "
            "non-admin token may only name the merchant in its own merchant_id "
            "claim and otherwise gets 403 'cannot run catalog jobs for another "
            "merchant'. Mint one with scripts/mint_employee_jwt.py --role admin."
        ))
    parser.add_argument("--internal-key", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--sync-limit", type=int, default=1)
    parser.add_argument("--sync-wait-seconds", type=float, default=20.0)
    parser.add_argument("--sync-poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--backfill-limit", type=int, default=1)
    parser.add_argument("--backfill-sample-limit", type=int, default=5)
    parser.add_argument("--backfill-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--payment-amount-minor", type=int, default=100)
    parser.add_argument("--payment-currency", default="USD")
    parser.add_argument("--payment-order-id", default="codex_signoff_canary")
    parser.add_argument("--payment-customer-email", default="ops+signoff@pivota.invalid")
    parser.add_argument("--payment-customer-name", default="Codex Signoff")
    parser.add_argument("--payment-description", default="codex commerce signoff")
    parser.add_argument("--payment-preferred-provider", default="stripe")
    parser.add_argument("--payment-label", default="codex_signoff")
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


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_body(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return _redact_sensitive(payload)
        return _redact_sensitive({"value": payload})
    except Exception:
        return {"raw_text": response.text[:2000]}


def _record_step(
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
        "ok": bool(ok),
        "elapsed_ms": round(float(elapsed_ms), 1),
        "status_code": status_code,
        "body": body,
    }
    if extra:
        record.update(extra)
    steps.append(record)
    return record


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in {"client_secret", "access_token", "refresh_token", "token"} or key_lower.endswith("_secret"):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _request_step(
    *,
    session: requests.Session,
    steps: List[Dict[str, Any]],
    name: str,
    method: str,
    url: str,
    ok_if=None,
    **kwargs: Any,
) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        response = session.request(method, url, **kwargs)
        body = _json_body(response)
        ok = 200 <= response.status_code < 300
        if ok_if is not None:
            ok = bool(ok_if(response.status_code, body))
        return _record_step(
            steps=steps,
            name=name,
            ok=ok,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            status_code=response.status_code,
            body=body,
        )
    except Exception as exc:
        return _record_step(
            steps=steps,
            name=name,
            ok=False,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            status_code=None,
            body={"error": str(exc)},
        )


def _connect_postgres(database_url: str):
    try:
        import psycopg  # type: ignore

        return psycopg.connect(database_url)
    except Exception:
        import psycopg2  # type: ignore

        return psycopg2.connect(database_url)


def _derive_query(database_url: str, merchant_id: str) -> str:
    with _connect_postgres(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT product_data
                FROM products_cache
                WHERE merchant_id = %s
                ORDER BY cached_at DESC
                LIMIT 50
                """,
                (merchant_id,),
            )
            rows = cursor.fetchall()
    for (product_data,) in rows:
        payload = _json_dict(product_data)
        title = str(payload.get("title") or "").strip()
        if title:
            return title
    raise RuntimeError(f"No product title found in products_cache for merchant_id={merchant_id}")


def _run_backfill_subprocess(
    *,
    database_url: str,
    merchant_id: str,
    platform: Optional[str],
    mode: str,
    limit: int,
    sample_limit: int,
    timeout_seconds: float,
) -> Dict[str, Any]:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    existing_pythonpath = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(REPO_ROOT)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "catalog_backfill_verify.py"),
        "--merchant-id",
        merchant_id,
        "--mode",
        mode,
        "--limit",
        str(limit),
        "--sample-limit",
        str(sample_limit),
        "--source-system",
        "products_cache_backfill_signoff",
        "--source-ref",
        f"products_cache_backfill_signoff:{int(time.time())}",
    ]
    if platform:
        cmd.extend(["--platform", platform])
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "body": {
                "error": f"timeout after {timeout_seconds}s",
                "stdout": (exc.stdout or "")[-2000:],
                "stderr": (exc.stderr or "")[-2000:],
            },
        }
    body: Dict[str, Any]
    stdout = (completed.stdout or "").strip()
    if stdout:
        try:
            parsed = json.loads(stdout)
            body = parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            body = {
                "raw_stdout": stdout[-4000:],
                "raw_stderr": (completed.stderr or "")[-4000:],
            }
    else:
        body = {"raw_stderr": (completed.stderr or "")[-4000:]}
    return {
        "returncode": completed.returncode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "body": body,
    }


def _backfill_ok(mode: str, payload: Dict[str, Any]) -> bool:
    if payload.get("returncode") != 0:
        return False
    summary = (payload.get("body") or {}).get("summary") or {}
    verify = summary.get("verify") or {}
    if mode == "apply":
        apply_stats = summary.get("apply_stats") or {}
        return int(apply_stats.get("products_failed") or 0) == 0 and int(verify.get("missing_product_keys_count") or 0) == 0
    return int(verify.get("missing_product_keys_count") or 0) == 0


def _platform_from_product_key(product_key: Any) -> Optional[str]:
    raw = str(product_key or "").strip()
    if not raw:
        return None
    parts = raw.split("::")
    if len(parts) >= 4 and parts[2].strip():
        return parts[2].strip().lower()
    return None


def _detect_catalog_platform(*, database_url: str, merchant_id: str, read_body: Dict[str, Any]) -> str:
    items = read_body.get("items") if isinstance(read_body, dict) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            product = item.get("product") if isinstance(item.get("product"), dict) else {}
            detected = _platform_from_product_key(product.get("product_key"))
            if detected:
                return detected
            merchant = item.get("merchant") if isinstance(item.get("merchant"), dict) else {}
            primary_platform = str(merchant.get("primary_platform") or "").strip().lower()
            if primary_platform:
                return primary_platform

    try:
        with _connect_postgres(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT platform
                    FROM merchant_stores
                    WHERE merchant_id = %s
                      AND status IN ('active', 'connected')
                    ORDER BY connected_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (merchant_id,),
                )
                row = cursor.fetchone()
        if row and row[0]:
            return str(row[0]).strip().lower()
    except Exception:
        pass
    return "shopify"


def _fetch_merchant_commerce_readiness_evidence(
    *,
    database_url: str,
    merchant_id: str,
) -> Dict[str, Any]:
    try:
        with _connect_postgres(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        merchant_id,
                        primary_platform,
                        active_psp,
                        foundation_status,
                        discover_status,
                        signals_status,
                        execute_status,
                        foundation_blockers,
                        discover_blockers,
                        signals_blockers,
                        execute_blockers,
                        surfaced_exposure_supported,
                        first_store_connected_at,
                        first_catalog_synced_at,
                        first_discover_ready_at,
                        days_to_discover_ready,
                        observed_at,
                        metadata
                    FROM merchant_commerce_readiness_state
                    WHERE merchant_id = %s
                    """,
                    (merchant_id,),
                )
                readiness_row = cursor.fetchone()
                readiness_columns = [col[0] for col in cursor.description] if cursor.description else []

                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM commerce_interactions WHERE merchant_id = %s) AS interaction_count,
                        (SELECT COUNT(*) FROM commerce_interaction_events WHERE merchant_id = %s) AS event_count,
                        (SELECT COUNT(*) FROM commerce_interaction_events WHERE merchant_id = %s AND event_type = 'surface.click') AS surface_click_event_count,
                        (SELECT COUNT(*) FROM commerce_interaction_events WHERE merchant_id = %s AND event_type = 'order.created') AS ordered_event_count,
                        (SELECT COUNT(*) FROM commerce_interaction_events WHERE merchant_id = %s AND event_type = 'refund.succeeded') AS refunded_event_count,
                        (SELECT COUNT(*) FROM surface_listing_states WHERE merchant_id = %s) AS listing_row_count,
                        (SELECT COUNT(*) FROM surface_click_events WHERE merchant_id = %s) AS click_row_count,
                        (SELECT COUNT(*) FROM commerce_attribution_edges WHERE merchant_id = %s) AS attribution_edge_count
                    """,
                    (
                        merchant_id,
                        merchant_id,
                        merchant_id,
                        merchant_id,
                        merchant_id,
                        merchant_id,
                        merchant_id,
                        merchant_id,
                    ),
                )
                ledger_row = cursor.fetchone()
                ledger_columns = [col[0] for col in cursor.description] if cursor.description else []
    except Exception as exc:
        return {
            "available": False,
            "reason": f"query_failed:{exc}",
            "readiness_state": None,
            "domain_statuses": {},
            "blocker_counts": {},
            "ledger_counts": {},
        }

    readiness_state = (
        {key: value for key, value in zip(readiness_columns, readiness_row)}
        if readiness_row and readiness_columns
        else None
    )
    ledger_counts = (
        {key: value for key, value in zip(ledger_columns, ledger_row)}
        if ledger_row and ledger_columns
        else {}
    )
    domain_statuses = {
        "foundation": (readiness_state or {}).get("foundation_status"),
        "discover": (readiness_state or {}).get("discover_status"),
        "signals": (readiness_state or {}).get("signals_status"),
        "execute": (readiness_state or {}).get("execute_status"),
    }
    blocker_counts = {
        "foundation": len((readiness_state or {}).get("foundation_blockers") or []),
        "discover": len((readiness_state or {}).get("discover_blockers") or []),
        "signals": len((readiness_state or {}).get("signals_blockers") or []),
        "execute": len((readiness_state or {}).get("execute_blockers") or []),
    }
    return {
        "available": readiness_state is not None,
        "reason": None if readiness_state is not None else "missing_readiness_row",
        "readiness_state": readiness_state,
        "domain_statuses": domain_statuses,
        "blocker_counts": blocker_counts,
        "ledger_counts": ledger_counts,
    }


def _refresh_merchant_commerce_readiness_via_api(
    *,
    session: requests.Session,
    steps: List[Dict[str, Any]],
    base_url: str,
    headers: Dict[str, str],
    timeout_seconds: float,
) -> Dict[str, Any]:
    return _request_step(
        session=session,
        steps=steps,
        name="merchant_commerce_readiness_refresh",
        method="GET",
        url=f"{base_url}/merchant/analytics/readiness-state",
        headers=headers,
        timeout=timeout_seconds,
        ok_if=lambda status, body: status == 200 and str(body.get("merchant_id") or "").strip() != "",
    )


def _render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Commerce Channels Signoff",
        "",
        f"- base_url: `{report['base_url']}`",
        f"- merchant_id: `{report['merchant_id']}`",
        f"- catalog_platform: `{report.get('catalog_platform')}`",
        f"- query: `{report['query']}`",
        f"- overall_ok: `{report['overall_ok']}`",
        "",
        "## Channels",
        "",
        f"- catalog_read_ok: `{summary.get('catalog_read_ok')}`",
        f"- catalog_write_ok: `{summary.get('catalog_write_ok')}`",
        f"- payment_order_ok: `{summary.get('payment_order_ok')}`",
        f"- readiness_state_available: `{summary.get('readiness_state_available')}`",
        "",
        "## Steps",
        "",
    ]
    for step in report.get("steps") or []:
        lines.append(
            f"- `{step['step']}` status=`{step.get('status_code')}` elapsed_ms=`{step.get('elapsed_ms')}` ok=`{step.get('ok')}`"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    query = str(args.query or "").strip() or _derive_query(args.database_url, args.merchant_id)
    headers = _headers(args.header)
    base_url = args.base_url.rstrip("/")
    steps: List[Dict[str, Any]] = []
    session = requests.Session()

    read_step = _request_step(
        session=session,
        steps=steps,
        name="catalog_read_query",
        method="POST",
        url=f"{base_url}/v1/pivot/query",
        headers=headers,
        timeout=args.timeout_seconds,
        json={
            "merchant_id": args.merchant_id,
            "query": query,
            "limit": 5,
            "include_external": False,
            "include_incentives": True,
        },
        ok_if=lambda status, body: status == 200 and int(body.get("total") or 0) > 0,
    )
    catalog_platform = _detect_catalog_platform(
        database_url=args.database_url,
        merchant_id=args.merchant_id,
        read_body=read_step.get("body") or {},
    )

    if catalog_platform == "shopify":
        webhook_step = _request_step(
            session=session,
            steps=steps,
            name="catalog_webhook_ingest",
            method="POST",
            url=f"{base_url}/v1/catalog/connectors/shopify/webhooks",
            headers=headers,
            timeout=args.timeout_seconds,
            params={
                "merchant_id": args.merchant_id,
                "event_type": "signoff_probe",
                "topic": "products/update",
                "source_ref": "codex_signoff_probe",
            },
            json={"smoke": True, "query": query},
            ok_if=lambda status, body: status == 200 and str(body.get("event_id") or "").strip() != "",
        )
    else:
        webhook_step = _record_step(
            steps=steps,
            name="catalog_webhook_ingest",
            ok=True,
            elapsed_ms=0.0,
            status_code=None,
            body={
                "skipped": True,
                "reason": "no_platform_webhook_route",
                "platform": catalog_platform,
            },
        )

    sync_create_step = _request_step(
        session=session,
        steps=steps,
        name="catalog_sync_job_create",
        method="POST",
        url=f"{base_url}/v1/catalog/sync/jobs",
        headers=headers,
        timeout=args.timeout_seconds,
        json={
            "merchant_id": args.merchant_id,
            "connector": catalog_platform,
            "mode": "reconcile",
            "platform": catalog_platform,
            "force_refresh": False,
            "limit": args.sync_limit,
            "sync_from_cache": True,
        },
        ok_if=lambda status, body: status == 200 and str(body.get("job_id") or "").strip() != "",
    )

    final_sync_step = None
    job_id = str((sync_create_step.get("body") or {}).get("job_id") or "").strip()
    if job_id:
        deadline = time.monotonic() + max(0.0, float(args.sync_wait_seconds))
        last_step = None
        while True:
            last_step = _request_step(
                session=session,
                steps=[],
                name="catalog_sync_job_poll",
                method="GET",
                url=f"{base_url}/v1/catalog/sync/jobs/{job_id}",
                headers=headers,
                timeout=args.timeout_seconds,
            )
            status = str((last_step.get("body") or {}).get("status") or "").lower()
            if status in {"completed", "failed"}:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.0, float(args.sync_poll_interval_seconds)))
        final_sync_step = _record_step(
            steps=steps,
            name="catalog_sync_job_final",
            ok=bool(last_step and last_step.get("status_code") == 200 and str((last_step.get("body") or {}).get("status") or "").lower() == "completed"),
            elapsed_ms=float((last_step or {}).get("elapsed_ms") or 0.0),
            status_code=last_step.get("status_code") if last_step else None,
            body=(last_step or {}).get("body") or {},
        )
    else:
        final_sync_step = _record_step(
            steps=steps,
            name="catalog_sync_job_final",
            ok=False,
            elapsed_ms=0.0,
            status_code=None,
            body={"error": "job_id_missing"},
        )

    backfill_apply = _run_backfill_subprocess(
        database_url=args.database_url,
        merchant_id=args.merchant_id,
        platform=catalog_platform,
        mode="apply",
        limit=args.backfill_limit,
        sample_limit=args.backfill_sample_limit,
        timeout_seconds=max(args.backfill_timeout_seconds, args.timeout_seconds, args.sync_wait_seconds),
    )
    backfill_apply_step = _record_step(
        steps=steps,
        name="catalog_backfill_apply",
        ok=_backfill_ok("apply", backfill_apply),
        elapsed_ms=float(backfill_apply.get("elapsed_ms") or 0.0),
        status_code=backfill_apply.get("returncode"),
        body=backfill_apply.get("body") or {},
    )

    backfill_verify = _run_backfill_subprocess(
        database_url=args.database_url,
        merchant_id=args.merchant_id,
        platform=catalog_platform,
        mode="verify",
        limit=args.backfill_limit,
        sample_limit=args.backfill_sample_limit,
        timeout_seconds=max(args.backfill_timeout_seconds, args.timeout_seconds, args.sync_wait_seconds),
    )
    backfill_verify_step = _record_step(
        steps=steps,
        name="catalog_backfill_verify",
        ok=_backfill_ok("verify", backfill_verify),
        elapsed_ms=float(backfill_verify.get("elapsed_ms") or 0.0),
        status_code=backfill_verify.get("returncode"),
        body=backfill_verify.get("body") or {},
    )

    payment_step = _request_step(
        session=session,
        steps=steps,
        name="payment_order_backed_canary",
        method="POST",
        url=f"{base_url}/payment/internal/canary/merchants/{args.merchant_id}/order-backed/execute",
        headers={
            "Content-Type": "application/json",
            "X-Pivota-Internal-Key": str(args.internal_key),
        },
        timeout=args.timeout_seconds,
        json={
            "amount": args.payment_amount_minor,
            "currency": args.payment_currency,
            "order_id": args.payment_order_id,
            "customer_email": args.payment_customer_email,
            "customer_name": args.payment_customer_name,
            "description": args.payment_description,
            "preferred_provider": args.payment_preferred_provider,
            "emit_merchant_webhook": False,
            "enforce_live_readiness": True,
            "label": args.payment_label,
        },
        ok_if=lambda status, body: status == 200 and bool(body.get("success")),
    )
    _refresh_merchant_commerce_readiness_via_api(
        session=session,
        steps=steps,
        base_url=base_url,
        headers=headers,
        timeout_seconds=args.timeout_seconds,
    )
    readiness_evidence = _fetch_merchant_commerce_readiness_evidence(
        database_url=args.database_url,
        merchant_id=args.merchant_id,
    )
    readiness_step = _record_step(
        steps=steps,
        name="merchant_commerce_readiness_snapshot",
        ok=bool(readiness_evidence.get("available")),
        elapsed_ms=0.0,
        status_code=None,
        body=readiness_evidence,
    )

    summary = {
        "catalog_read_ok": bool(read_step.get("ok")),
        "catalog_write_ok": all(
            step.get("ok")
            for step in (
                webhook_step,
                sync_create_step,
                final_sync_step,
                backfill_apply_step,
                backfill_verify_step,
            )
        ),
        "payment_order_ok": bool(payment_step.get("ok")),
        "readiness_state_available": bool(readiness_step.get("ok")),
        "readiness_domain_statuses": readiness_evidence.get("domain_statuses") or {},
        "readiness_blocker_counts": readiness_evidence.get("blocker_counts") or {},
    }
    report = {
        "base_url": base_url,
        "merchant_id": args.merchant_id,
        "catalog_platform": catalog_platform,
        "query": query,
        "readiness_state": readiness_evidence.get("readiness_state"),
        "ledger_counts": readiness_evidence.get("ledger_counts") or {},
        "overall_ok": all(
            bool(summary.get(key))
            for key in ("catalog_read_ok", "catalog_write_ok", "payment_order_ok")
        ),
        "summary": summary,
        "steps": steps,
    }
    json_blob = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    markdown = _render_markdown(report)
    _write_if_requested(args.output_json, json_blob + "\n")
    _write_if_requested(args.output_md, markdown)
    print(json_blob)
    return 0 if report["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
