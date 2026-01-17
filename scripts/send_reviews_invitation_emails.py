#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import psycopg2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _connect_db(database_url: str):
    return psycopg2.connect(database_url)


def _reviews_base_url() -> str:
    return (os.getenv("REVIEWS_BASE_URL") or os.getenv("BASE_URL") or "").strip().rstrip("/")


def _internal_key() -> str:
    return (
        (os.getenv("REVIEWS_INVITATION_ISSUER_INTERNAL_KEY") or "").strip()
        or (os.getenv("REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY") or "").strip()
        or (os.getenv("REVIEWS_PROOF_ISSUER_INTERNAL_KEY") or "").strip()
    )


@dataclass(frozen=True)
class Plan:
    delivered_after_days: int
    shipped_after_days: int
    paid_after_days: int
    ttl_seconds: int
    max_links: int
    limit: int


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Send buyer review invitation emails for eligible paid orders (safe dry-run by default)."
    )
    p.add_argument("--database-url", default=os.getenv("DATABASE_URL") or "", help="Postgres connection string.")
    p.add_argument("--reviews-base-url", default=_reviews_base_url(), help="Reviews backend base URL.")
    p.add_argument("--internal-key", default=_internal_key(), help="X-Internal-Key for invitation issuer endpoints.")
    p.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run).")
    p.add_argument("--delivered-after-days", type=int, default=3, help="Send N days after delivered_at.")
    p.add_argument("--shipped-after-days", type=int, default=10, help="Fallback: send N days after shipped_at.")
    p.add_argument("--paid-after-days", type=int, default=14, help="Fallback: send N days after paid_at.")
    p.add_argument("--ttl-seconds", type=int, default=7 * 24 * 3600, help="Invitation token TTL seconds.")
    p.add_argument("--max-links", type=int, default=3, help="Max invitation links per email.")
    p.add_argument("--limit", type=int, default=50, help="Max orders per run.")
    return p.parse_args()


def _build_plan(args: argparse.Namespace) -> Plan:
    return Plan(
        delivered_after_days=max(0, int(args.delivered_after_days)),
        shipped_after_days=max(0, int(args.shipped_after_days)),
        paid_after_days=max(0, int(args.paid_after_days)),
        ttl_seconds=max(300, int(args.ttl_seconds)),
        max_links=max(1, min(10, int(args.max_links))),
        limit=max(1, min(500, int(args.limit))),
    )


def _select_candidates(cur, plan: Plan) -> List[Tuple[str, str]]:
    sql = """
    SELECT order_id, merchant_id
    FROM orders
    WHERE is_deleted IS FALSE
      AND (payment_status = 'paid' OR status IN ('paid','shipped','delivered'))
      AND COALESCE(customer_email, '') <> ''
      AND COALESCE(metadata->>'reviews_invitation_email_sent_at', '') = ''
      AND COALESCE(metadata->>'reviews_invitation_email_sending_at', '') = ''
      AND (
        (delivered_at IS NOT NULL AND delivered_at <= now() - (%s || ' days')::interval)
        OR
        (delivered_at IS NULL AND shipped_at IS NOT NULL AND shipped_at <= now() - (%s || ' days')::interval)
        OR
        (delivered_at IS NULL AND shipped_at IS NULL AND paid_at IS NOT NULL AND paid_at <= now() - (%s || ' days')::interval)
      )
    ORDER BY COALESCE(delivered_at, shipped_at, paid_at, created_at) ASC
    LIMIT %s
    """
    cur.execute(
        sql,
        (
            int(plan.delivered_after_days),
            int(plan.shipped_after_days),
            int(plan.paid_after_days),
            int(plan.limit),
        ),
    )
    rows = cur.fetchall() or []
    out: List[Tuple[str, str]] = []
    for r in rows:
        try:
            oid = str(r[0] or "").strip()
            mid = str(r[1] or "").strip()
        except Exception:
            continue
        if oid and mid:
            out.append((oid, mid))
    return out


def _lock_sending(cur, order_id: str) -> bool:
    patch = {"reviews_invitation_email_sending_at": _iso_utc_now(), "reviews_invitation_email_sending_v": 1}
    sql = """
    UPDATE orders
    SET
      metadata = (COALESCE(metadata::jsonb, '{}'::jsonb) || %s::jsonb)::jsonb,
      updated_at = now()
    WHERE order_id = %s
      AND COALESCE(metadata->>'reviews_invitation_email_sent_at', '') = ''
      AND COALESCE(metadata->>'reviews_invitation_email_sending_at', '') = ''
    """
    cur.execute(sql, (json.dumps(patch, separators=(",", ":")), order_id))
    return bool(cur.rowcount and int(cur.rowcount) > 0)


def _clear_sending(cur, order_id: str) -> None:
    sql = """
    UPDATE orders
    SET
      metadata = (COALESCE(metadata::jsonb, '{}'::jsonb) - 'reviews_invitation_email_sending_at' - 'reviews_invitation_email_sending_v')::jsonb,
      updated_at = now()
    WHERE order_id = %s
    """
    cur.execute(sql, (order_id,))


def _mark_sent(cur, order_id: str, *, subject_count: int) -> None:
    patch = {
        "reviews_invitation_email_sent_at": _iso_utc_now(),
        "reviews_invitation_email_sent_v": 1,
        "reviews_invitation_email_sent_subject_count": int(subject_count),
    }
    sql = """
    UPDATE orders
    SET
      metadata = ((COALESCE(metadata::jsonb, '{}'::jsonb) - 'reviews_invitation_email_sending_at' - 'reviews_invitation_email_sending_v') || %s::jsonb)::jsonb,
      updated_at = now()
    WHERE order_id = %s
    """
    cur.execute(sql, (json.dumps(patch, separators=(",", ":")), order_id))


def _post_send_email(
    *,
    base_url: str,
    internal_key: str,
    merchant_id: str,
    order_id: str,
    ttl_seconds: int,
    max_links: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/internal/reviews/v1/invitation/send-email-from-order"
    headers = {"X-Internal-Key": internal_key, "Content-Type": "application/json"}
    body = {
        "merchant_id": merchant_id,
        "order_id": order_id,
        "ttl_seconds": int(ttl_seconds),
        "max_links": int(max_links),
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(url, headers=headers, json=body)
    except Exception:
        return {"ok": False, "http_status": 0, "error": "UNAVAILABLE"}

    try:
        data = resp.json()
    except Exception:
        data = {}

    return {"ok": resp.status_code == 200, "http_status": resp.status_code, "data": data}


def main() -> int:
    args = _parse_args()
    if not args.database_url:
        print("ERROR: missing --database-url (or env DATABASE_URL)")
        return 2
    if not args.reviews_base_url:
        print("ERROR: missing --reviews-base-url (or env REVIEWS_BASE_URL)")
        return 2
    if not args.internal_key:
        print("ERROR: missing --internal-key (or env REVIEWS_INVITATION_ISSUER_INTERNAL_KEY)")
        return 2

    plan = _build_plan(args)
    mode = "apply" if args.apply else "dry_run"
    print(f"mode={mode}")
    print(f"delivered_after_days={plan.delivered_after_days}")
    print(f"shipped_after_days={plan.shipped_after_days}")
    print(f"paid_after_days={plan.paid_after_days}")
    print(f"ttl_seconds={plan.ttl_seconds}")
    print(f"max_links={plan.max_links}")
    print(f"limit={plan.limit}")

    conn = _connect_db(args.database_url)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            candidates = _select_candidates(cur, plan)
        conn.rollback()

        print(f"candidates={len(candidates)}")
        if not candidates:
            print("ok=1")
            return 0

        sample = [oid for (oid, _mid) in candidates[:10]]
        print("sample_order_ids=" + ",".join(sample))

        if not args.apply:
            print("ok=1 (dry-run, no emails sent)")
            return 0

        sent = 0
        skipped = 0
        failed = 0

        for (order_id, merchant_id) in candidates:
            # 1) Lock
            with conn.cursor() as cur:
                locked = _lock_sending(cur, order_id)
            if not locked:
                conn.rollback()
                skipped += 1
                continue
            conn.commit()

            # 2) Send email via internal endpoint (server-side email send).
            result = _post_send_email(
                base_url=args.reviews_base_url,
                internal_key=args.internal_key,
                merchant_id=merchant_id,
                order_id=order_id,
                ttl_seconds=plan.ttl_seconds,
                max_links=plan.max_links,
            )

            if not result.get("ok"):
                with conn.cursor() as cur:
                    _clear_sending(cur, order_id)
                conn.commit()
                failed += 1
                print(f"send_failed order_id={order_id} http_status={result.get('http_status')}")
                continue

            data = result.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            sent_flag = bool(data.get("sent", True))
            reason = str(data.get("reason") or "ok")

            if not sent_flag:
                with conn.cursor() as cur:
                    _clear_sending(cur, order_id)
                conn.commit()
                skipped += 1
                print(f"send_skipped order_id={order_id} reason={reason}")
                continue

            subject_count = int(data.get("subject_count") or 1)
            with conn.cursor() as cur:
                _mark_sent(cur, order_id, subject_count=subject_count)
            conn.commit()
            sent += 1
            print(f"sent_ok order_id={order_id} subject_count={subject_count}")

        print(f"sent={sent} skipped={skipped} failed={failed}")
        print("ok=1")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

