#!/usr/bin/env python3
"""Production-safe signoff for the canonical commerce telemetry pipeline.

The default mode is read-only. ``--write-canary`` is required before the
harness submits a synthetic, clearly namespaced event chain to a merchant's
dedicated canary store scope.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

EXPECTED_EVENT_TYPES = (
    "agent.requested",
    "product.viewed",
    "cart.item_added",
    "checkout.started",
    "payment.attempted",
    "order.created",
    "payment.succeeded",
    "refund.succeeded",
)
EXPECTED_STAGES = (
    "agent_requested",
    "product_viewed",
    "cart_active",
    "checkout_started",
    "payment_attempted",
    "order_created",
    "paid",
    "refund_active",
    "refunded",
)
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "merchant_api_key",
        "merchant_jwt",
        "x-pivota-signature",
        "x-pivota-merchant-id",
        "cookie",
        "set-cookie",
    }
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit canonical commerce telemetry readiness and optionally write "
            "an idempotent end-to-end canary chain."
        )
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="Public API origin, for example https://api.pivota.cc",
    )
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument(
        "--platform", required=True, help="Canonical platform slug, for example cafe24"
    )
    parser.add_argument(
        "--store-id", required=True, help="Connected store scope used for the canary"
    )
    parser.add_argument(
        "--expected-git-sha",
        default=None,
        help="Optional deployed commit prefix required from GET /version.",
    )
    parser.add_argument(
        "--merchant-jwt",
        default=os.getenv("MERCHANT_JWT") or os.getenv("PIVOTA_MERCHANT_JWT"),
        help="Merchant bearer token used only for scoped analytics reads.",
    )
    parser.add_argument(
        "--merchant-api-key",
        default=os.getenv("PIVOTA_MERCHANT_API_KEY"),
        help="Merchant HMAC key. Required only with --write-canary.",
    )
    parser.add_argument(
        "--write-canary",
        action="store_true",
        help="Explicitly allow an eight-event synthetic write and idempotency replay.",
    )
    parser.add_argument(
        "--confirm-dedicated-canary-store",
        default=None,
        metavar="STORE_ID",
        help=(
            "Required with --write-canary. Must exactly match --store-id to confirm "
            "the target is dedicated to synthetic telemetry probes."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Stable run identifier. Reusing it verifies idempotent replay without adding events.",
    )
    parser.add_argument(
        "--amount-minor",
        type=int,
        default=100,
        help="Synthetic paid amount in minor units.",
    )
    parser.add_argument(
        "--refund-minor",
        type=int,
        default=25,
        help="Synthetic partial refund in minor units.",
    )
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--poll-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    args = parser.parse_args()
    if not args.merchant_jwt:
        parser.error("--merchant-jwt is required or set MERCHANT_JWT")
    if args.write_canary and not args.merchant_api_key:
        parser.error("--merchant-api-key is required with --write-canary")
    if args.write_canary and _text(args.confirm_dedicated_canary_store) != _text(
        args.store_id
    ):
        parser.error(
            "--confirm-dedicated-canary-store must exactly match --store-id "
            "when --write-canary is enabled"
        )
    if args.amount_minor < 0 or args.refund_minor < 0:
        parser.error("canary amounts must be non-negative")
    if args.refund_minor > args.amount_minor:
        parser.error("--refund-minor must not exceed --amount-minor")
    args.currency = str(args.currency).strip().upper()
    if len(args.currency) != 3:
        parser.error("--currency must be a three-letter code")
    args.platform = str(args.platform).strip().lower().replace(" ", "_")
    args.run_id = str(args.run_id or _new_run_id()).strip()
    return args


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{secrets.token_hex(4)}"


def _base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("--base-url must be an HTTPS origin without a path")
    return raw


def _text(value: Any) -> str:
    return str(value or "").strip()


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in SENSITIVE_KEYS or normalized.endswith("_secret"):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_sensitive(child)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(child) for child in value]
    return value


def _json_body(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {"raw_text": str(getattr(response, "text", ""))[:2000]}
    if isinstance(payload, dict):
        return _redact_sensitive(payload)
    return {"value": _redact_sensitive(payload)}


def _build_canary_events(args: argparse.Namespace) -> List[Dict[str, Any]]:
    prefix = (
        "telemetry_canary_"
        + hashlib.sha256(
            f"{args.merchant_id}|{args.platform}|{args.store_id}|{args.run_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:20]
    )
    occurred_at = datetime.now(timezone.utc).isoformat()
    interaction_id = f"int_{prefix}"
    session_id = f"ses_{prefix}"
    click_id = f"clk_{prefix}"
    cart_id = f"cart_{prefix}"
    checkout_id = f"checkout_{prefix}"
    payment_id = f"payment_{prefix}"
    order_id = f"order_{prefix}"
    refund_id = f"refund_{prefix}"
    common = {
        "occurred_at": occurred_at,
        "platform": args.platform,
        "source": "ops_commerce_telemetry_canary",
        "store_id": args.store_id,
        "surface": "ops_canary",
        "interaction_id": interaction_id,
        "session_id": session_id,
        "click_id": click_id,
        "source_channel": "ops_canary",
    }

    def event(event_type: str, **fields: Any) -> Dict[str, Any]:
        return {
            **common,
            "event_id": f"{prefix}:{event_type}",
            "event_type": event_type,
            **fields,
        }

    return [
        event("agent.requested", agent_id="pivota_ops_canary"),
        event("product.viewed", canonical_product_id="canary_product"),
        event(
            "cart.item_added",
            cart_id=cart_id,
            canonical_product_id="canary_product",
            metadata={"quantity": 1},
        ),
        event("checkout.started", cart_id=cart_id, checkout_id=checkout_id),
        event("payment.attempted", checkout_id=checkout_id, payment_id=payment_id),
        event(
            "order.created",
            checkout_id=checkout_id,
            payment_id=payment_id,
            order_id=order_id,
            amount_cents=args.amount_minor,
            currency=args.currency,
        ),
        event(
            "payment.succeeded",
            checkout_id=checkout_id,
            payment_id=payment_id,
            order_id=order_id,
            amount_cents=args.amount_minor,
            currency=args.currency,
        ),
        event(
            "refund.succeeded",
            payment_id=payment_id,
            order_id=order_id,
            refund_id=refund_id,
            amount_cents=args.refund_minor,
            currency=args.currency,
        ),
    ]


def _signed_batch(events: Iterable[Dict[str, Any]], api_key: str) -> tuple[bytes, str]:
    body = json.dumps(
        {"events": list(events)}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    signature = hmac.new(str(api_key).encode("utf-8"), body, hashlib.sha256).hexdigest()
    return body, signature


def _event_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = payload.get("event_funnel")
    if not isinstance(value, dict):
        return {}
    summary = value.get("summary")
    return summary if isinstance(summary, dict) else {}


def _deployment_sha(payload: Dict[str, Any]) -> str:
    return _text(payload.get("full_sha") or payload.get("version")).lower()


def _trace_event_types(payload: Dict[str, Any]) -> set[str]:
    return {
        _text(row.get("event_type")).lower()
        for row in payload.get("events") or []
        if isinstance(row, dict) and _text(row.get("event_type"))
    }


def _connected_store(
    payload: Dict[str, Any], *, store_id: str, platform: str
) -> Optional[Dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for row in data.get("stores") or []:
        if not isinstance(row, dict):
            continue
        if _text(row.get("id") or row.get("store_id")) != store_id:
            continue
        if _text(row.get("platform")).lower() != platform:
            continue
        if bool(row.get("is_active")) and bool(row.get("is_connected")):
            return row
    return None


def _record(
    steps: List[Dict[str, Any]],
    *,
    name: str,
    ok: bool,
    response: Optional[requests.Response] = None,
    body: Optional[Dict[str, Any]] = None,
    detail: Optional[str] = None,
) -> bool:
    steps.append(
        {
            "step": name,
            "ok": bool(ok),
            "status_code": getattr(response, "status_code", None),
            "detail": detail,
            "body": _redact_sensitive(
                body
                if body is not None
                else (_json_body(response) if response is not None else {})
            ),
        }
    )
    return bool(ok)


def _analytics_params(args: argparse.Namespace) -> Dict[str, str]:
    params = {
        "group_by": "store",
        "platform": args.platform,
        "store_id": args.store_id,
    }
    if args.write_canary:
        params["surface"] = "ops_canary"
    return params


def _merchant_read_headers(args: argparse.Namespace) -> Dict[str, str]:
    return {"Authorization": f"Bearer {args.merchant_jwt}"}


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Commerce telemetry canary",
        "",
        f"- Overall: **{'PASS' if report.get('overall_ok') else 'FAIL'}**",
        f"- Mode: `{report.get('mode')}`",
        f"- Merchant: `{report.get('merchant_id')}`",
        f"- Platform/store: `{report.get('platform')}` / `{report.get('store_id')}`",
        f"- Run: `{report.get('run_id')}`",
        "",
        "| Step | Result | HTTP | Detail |",
        "| --- | --- | ---: | --- |",
    ]
    for step in report.get("steps") or []:
        detail = _text(step.get("detail")).replace("|", "\\|")
        lines.append(
            f"| `{step.get('step')}` | {'PASS' if step.get('ok') else 'FAIL'} | "
            f"{step.get('status_code') or ''} | {detail} |"
        )
    return "\n".join(lines) + "\n"


def _write_outputs(args: argparse.Namespace, report: Dict[str, Any]) -> None:
    if args.output_json:
        path = Path(args.output_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_redact_sensitive(report), indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    if args.output_md:
        path = Path(args.output_md)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(report), encoding="utf-8")


def run(
    args: argparse.Namespace,
    *,
    session: Optional[requests.Session] = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> Dict[str, Any]:
    if args.write_canary and _text(
        getattr(args, "confirm_dedicated_canary_store", None)
    ) != _text(args.store_id):
        raise ValueError(
            "write canary requires confirmation that --store-id is a dedicated "
            "canary store"
        )
    base = _base_url(args.base_url)
    client = session or requests.Session()
    client.headers.update({"User-Agent": "pivota-commerce-telemetry-canary/1.0"})
    read_headers = _merchant_read_headers(args)
    steps: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "write" if args.write_canary else "audit",
        "base_url": base,
        "merchant_id": args.merchant_id,
        "platform": args.platform,
        "store_id": args.store_id,
        "run_id": args.run_id,
        "steps": steps,
    }

    health = client.get(f"{base}/health", timeout=args.timeout_seconds)
    _record(
        steps, name="deployment_health", ok=health.status_code == 200, response=health
    )

    version_response = client.get(f"{base}/version", timeout=args.timeout_seconds)
    version_body = _json_body(version_response)
    deployed_sha = _deployment_sha(version_body)
    expected_sha = _text(args.expected_git_sha).lower()
    version_ok = version_response.status_code == 200 and deployed_sha not in {
        "",
        "unknown",
    }
    if expected_sha:
        version_ok = version_ok and deployed_sha.startswith(expected_sha)
    _record(
        steps,
        name="deployment_version",
        ok=version_ok,
        response=version_response,
        body=version_body,
        detail=(
            f"deployed={deployed_sha or 'unknown'} expected_prefix={expected_sha or 'any-known-sha'}"
        ),
    )

    stores_response = client.get(
        f"{base}/merchant/{args.merchant_id}/integrations",
        headers=read_headers,
        timeout=args.timeout_seconds,
    )
    stores_body = _json_body(stores_response)
    selected_store = _connected_store(
        stores_body,
        store_id=args.store_id,
        platform=args.platform,
    )
    _record(
        steps,
        name="connected_store_scope",
        ok=stores_response.status_code == 200 and selected_store is not None,
        response=stores_response,
        body={
            "store": (
                {
                    key: selected_store.get(key)
                    for key in (
                        "id",
                        "platform",
                        "name",
                        "domain",
                        "status",
                        "is_active",
                        "is_connected",
                    )
                }
                if selected_store
                else None
            )
        },
        detail=(
            None if selected_store else "active connected store/platform pair not found"
        ),
    )

    baseline_response = client.get(
        f"{base}/merchant/analytics/commerce-funnel",
        params=_analytics_params(args),
        headers=read_headers,
        timeout=args.timeout_seconds,
    )
    baseline = _json_body(baseline_response)
    baseline_available = bool((baseline.get("event_funnel") or {}).get("available"))
    _record(
        steps,
        name="canonical_funnel_available",
        ok=baseline_response.status_code == 200 and baseline_available,
        response=baseline_response,
        body={"event_funnel": baseline.get("event_funnel")},
        detail=(
            None
            if baseline_available
            else "canonical event store unavailable or response invalid"
        ),
    )

    issues_response = client.get(
        f"{base}/merchant/analytics/commerce-funnel/issues",
        params={"limit": "50"},
        headers=read_headers,
        timeout=args.timeout_seconds,
    )
    _record(
        steps,
        name="funnel_diagnostics_readable",
        ok=issues_response.status_code == 200,
        response=issues_response,
        body=_json_body(issues_response),
    )

    if (
        args.write_canary
        and selected_store is not None
        and baseline_response.status_code == 200
        and baseline_available
    ):
        events = _build_canary_events(args)
        body_bytes, signature = _signed_batch(events, args.merchant_api_key)
        ingest_headers = {
            "Content-Type": "application/json",
            "X-Pivota-Merchant-Id": args.merchant_id,
            "X-Pivota-Signature": signature,
        }
        first = client.post(
            f"{base}/merchant-events/v1/batch",
            data=body_bytes,
            headers=ingest_headers,
            timeout=args.timeout_seconds,
        )
        first_body = _json_body(first)
        first_total = int(first_body.get("accepted") or 0) + int(
            first_body.get("duplicates") or 0
        )
        _record(
            steps,
            name="canonical_batch_ingest",
            ok=first.status_code == 200 and first_total == len(events),
            response=first,
            body=first_body,
            detail=f"accepted={first_body.get('accepted', 0)} duplicates={first_body.get('duplicates', 0)}",
        )

        replay = client.post(
            f"{base}/merchant-events/v1/batch",
            data=body_bytes,
            headers=ingest_headers,
            timeout=args.timeout_seconds,
        )
        replay_body = _json_body(replay)
        _record(
            steps,
            name="idempotent_replay",
            ok=replay.status_code == 200
            and int(replay_body.get("duplicates") or 0) == len(events),
            response=replay,
            body=replay_body,
            detail=f"duplicates={replay_body.get('duplicates', 0)} expected={len(events)}",
        )

        interaction_id = events[0]["interaction_id"]
        deadline = monotonic() + max(0.0, float(args.poll_timeout_seconds))
        trace_payload: Dict[str, Any] = {}
        trace_response: Optional[requests.Response] = None
        while True:
            trace_response = client.get(
                f"{base}/merchant/analytics/commerce-interactions/{interaction_id}",
                headers=read_headers,
                timeout=args.timeout_seconds,
            )
            trace_payload = _json_body(trace_response)
            if trace_response.status_code == 200 and set(EXPECTED_EVENT_TYPES).issubset(
                _trace_event_types(trace_payload)
            ):
                break
            if monotonic() >= deadline:
                break
            sleep(max(0.0, float(args.poll_interval_seconds)))
        observed_types = _trace_event_types(trace_payload)
        missing_types = sorted(set(EXPECTED_EVENT_TYPES) - observed_types)
        _record(
            steps,
            name="stitched_interaction_trace",
            ok=bool(trace_response)
            and trace_response.status_code == 200
            and not missing_types,
            response=trace_response,
            body={
                "interaction": trace_payload.get("interaction"),
                "event_types": sorted(observed_types),
            },
            detail="missing=" + (",".join(missing_types) if missing_types else "none"),
        )

        funnel_response = client.get(
            f"{base}/merchant/analytics/commerce-funnel",
            params=_analytics_params(args),
            headers=read_headers,
            timeout=args.timeout_seconds,
        )
        funnel_payload = _json_body(funnel_response)
        summary = _event_summary(funnel_payload)
        stages = (
            summary.get("stages") if isinstance(summary.get("stages"), dict) else {}
        )
        missing_stages = [
            stage for stage in EXPECTED_STAGES if int(stages.get(stage) or 0) < 1
        ]
        _record(
            steps,
            name="funnel_stage_materialization",
            ok=funnel_response.status_code == 200 and not missing_stages,
            response=funnel_response,
            body={"summary": summary},
            detail="missing="
            + (",".join(missing_stages) if missing_stages else "none"),
        )

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["overall_ok"] = bool(steps) and all(bool(step.get("ok")) for step in steps)
    _write_outputs(args, report)
    return report


def main() -> int:
    args = _parse_args()
    try:
        report = run(args)
    except (requests.RequestException, ValueError) as exc:
        report = {
            "overall_ok": False,
            "mode": "write" if args.write_canary else "audit",
            "merchant_id": args.merchant_id,
            "platform": args.platform,
            "store_id": args.store_id,
            "run_id": args.run_id,
            "error": str(exc),
            "steps": [],
        }
        _write_outputs(args, report)
    if not args.output_json:
        print(json.dumps(_redact_sensitive(report), indent=2, ensure_ascii=True))
    return 0 if report.get("overall_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
