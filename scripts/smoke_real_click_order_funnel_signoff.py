#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests


DEFAULT_SURFACE = "ucp"
DEFAULT_MARKET = "US"


def _utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Production-safe click -> order commerce funnel signoff. "
            "Generates one real attributed click, reuses its pvt_* values in "
            "POST /agent/v2/commerce/checkouts, and verifies merchant funnel/trace updates."
        )
    )
    parser.add_argument("--base-url", required=True, help="API base URL, e.g. https://api.pivota.cc")
    parser.add_argument(
        "--merchant-id",
        default=os.getenv("READINESS_ALPHA_MERCHANT_ID"),
    )
    parser.add_argument("--surface", default=os.getenv("COMMERCE_FUNNEL_SURFACE") or DEFAULT_SURFACE)
    parser.add_argument(
        "--analytics-surface",
        default=None,
        help="Optional surface filter for merchant analytics reads. Default: no filter.",
    )
    parser.add_argument("--market", default=DEFAULT_MARKET)
    parser.add_argument(
        "--tool",
        default=None,
        help="Outbound links tool. Defaults to --surface.",
    )
    parser.add_argument(
        "--internal-key",
        default=os.getenv("READINESS_INTERNAL_API_KEY") or os.getenv("READINESS_KEY") or None,
        help="Internal readiness key used for readiness report/export precheck.",
    )
    parser.add_argument(
        "--agent-api-key",
        default=os.getenv("PIVOTA_AGENT_API_KEY") or os.getenv("SHOP_GATEWAY_AGENT_API_KEY") or None,
        help="Agent API key for /agent/v2/commerce/checkouts.",
    )
    parser.add_argument(
        "--merchant-jwt",
        default=os.getenv("MERCHANT_JWT") or os.getenv("PIVOTA_MERCHANT_JWT") or None,
        help="Merchant JWT for /merchant/analytics/* reads.",
    )
    parser.add_argument("--internal-header", action="append", default=[], help="Repeatable raw header for internal readiness routes.")
    parser.add_argument("--agent-header", action="append", default=[], help="Repeatable raw header for agent commerce routes.")
    parser.add_argument("--merchant-header", action="append", default=[], help="Repeatable raw header for merchant analytics routes.")
    parser.add_argument("--offer-id", default=None, help="Optional readiness export offer_id override.")
    parser.add_argument("--product-id", default=None, help="Optional readiness export product_id override.")
    parser.add_argument("--variant-id", default=None, help="Optional readiness export variant_id override.")
    parser.add_argument("--sku-id", default=None, help="Optional outbound links skuId candidate.")
    parser.add_argument("--brand", default=None, help="Optional outbound links brand candidate override.")
    parser.add_argument("--category", default=None, help="Optional outbound links category candidate override.")
    parser.add_argument("--title", default=None, help="Optional order item title override.")
    parser.add_argument("--unit-price", type=float, default=None, help="Optional order item unit_price override.")
    parser.add_argument("--currency", default=None, help="Optional checkout currency override. Defaults to the readiness export currency.")
    parser.add_argument("--canonical-product-id", default=None, help="Optional canonical product id override.")
    parser.add_argument("--canonical-variant-id", default=None, help="Optional canonical variant id override.")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--prompt-cluster", default=None)
    parser.add_argument("--preferred-psp", default=None)
    parser.add_argument("--skip-impression", action="store_true", help="Skip POST /api/links/impression before the real click.")
    parser.add_argument("--buyer-email", default="ops-click-order@example.com")
    parser.add_argument("--customer-name", default="Pivota Click Order Canary")
    parser.add_argument("--address-name", default="Pivota Click Order Canary")
    parser.add_argument("--address-line1", default="1 Market St")
    parser.add_argument("--address-line2", default="")
    parser.add_argument("--city", default="San Francisco")
    parser.add_argument("--state", default="CA")
    parser.add_argument("--postal-code", default="94105")
    parser.add_argument("--country", default="US")
    parser.add_argument("--phone", default="")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--poll-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    args = parser.parse_args()
    if not args.merchant_id:
        parser.error("--merchant-id is required or set READINESS_ALPHA_MERCHANT_ID")
    args.run_id = str(args.run_id or _utc_now_compact())
    return args


def _headers(raw_headers: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for raw in raw_headers:
        if ":" not in str(raw):
            continue
        name, value = str(raw).split(":", 1)
        if name.strip():
            headers[name.strip()] = value.strip()
    return headers


def _has_header(headers: Dict[str, str], name: str) -> bool:
    target = name.strip().lower()
    return any(str(key).strip().lower() == target for key in headers)


def _ensure_auth_headers(args: argparse.Namespace) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    internal_headers = _headers(args.internal_header)
    agent_headers = _headers(args.agent_header)
    merchant_headers = _headers(args.merchant_header)

    if args.internal_key and not _has_header(internal_headers, "X-Pivota-Internal-Key") and not _has_header(internal_headers, "Authorization"):
        internal_headers["X-Pivota-Internal-Key"] = str(args.internal_key)
    if args.agent_api_key and not _has_header(agent_headers, "X-API-Key") and not _has_header(agent_headers, "Authorization"):
        agent_headers["X-API-Key"] = str(args.agent_api_key)
    if args.merchant_jwt and not _has_header(merchant_headers, "Authorization"):
        merchant_headers["Authorization"] = f"Bearer {args.merchant_jwt}"

    if not (_has_header(internal_headers, "X-Pivota-Internal-Key") or _has_header(internal_headers, "Authorization")):
        raise SystemExit("Missing internal readiness auth. Provide --internal-key or --internal-header.")
    if not (_has_header(agent_headers, "X-API-Key") or _has_header(agent_headers, "Authorization")):
        raise SystemExit("Missing agent auth. Provide --agent-api-key or --agent-header.")
    if not _has_header(merchant_headers, "Authorization"):
        raise SystemExit("Missing merchant analytics auth. Provide --merchant-jwt or --merchant-header.")

    return internal_headers, agent_headers, merchant_headers


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
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_body(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        text = getattr(response, "text", "")
        return {"raw_text": str(text)[:2000]}
    if isinstance(payload, dict):
        return _redact_sensitive(payload)
    return _redact_sensitive({"value": payload})


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _stable_id(prefix: str, *parts: Any) -> str:
    normalized = "|".join(_normalize_text(part).lower() for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _canonical_product_id(merchant_id: str, platform: str, product_id: str) -> str:
    return _stable_id("cp", merchant_id, platform, product_id)


def _canonical_variant_id(merchant_id: str, platform: str, product_id: str, variant_id: str) -> str:
    return _stable_id("cv", merchant_id, platform, product_id, variant_id)


def _interaction_id_from_click(merchant_id: str, click_id: str) -> str:
    return _stable_id("int", merchant_id, "click_id", click_id)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).strip().lower()
            if (
                key_lower in {"authorization", "x-api-key", "x-pivota-internal-key", "merchant_jwt", "agent_api_key", "internal_key"}
                or key_lower == "client_secret"
                or key_lower.endswith("_secret")
            ):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


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
        "body": _redact_sensitive(body),
    }
    if extra:
        record.update(_redact_sensitive(extra))
    steps.append(record)
    return record


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


def _funnel_query(base_url: str, analytics_surface: Optional[str]) -> str:
    if analytics_surface:
        return f"{base_url}/merchant/analytics/commerce-funnel?surface={analytics_surface}"
    return f"{base_url}/merchant/analytics/commerce-funnel"


def _issues_query(base_url: str, analytics_surface: Optional[str]) -> str:
    if analytics_surface:
        return f"{base_url}/merchant/analytics/commerce-funnel/issues?surface={analytics_surface}"
    return f"{base_url}/merchant/analytics/commerce-funnel/issues"


def _trace_query(base_url: str, interaction_id: str) -> str:
    return f"{base_url}/merchant/analytics/commerce-interactions/{interaction_id}"


def _offer_matches_args(offer: Dict[str, Any], args: argparse.Namespace) -> bool:
    if args.offer_id and _normalize_text(offer.get("offer_id")) != _normalize_text(args.offer_id):
        return False
    if args.product_id and _normalize_text(offer.get("product_id")) != _normalize_text(args.product_id):
        return False
    if args.variant_id and _normalize_text(offer.get("variant_id")) != _normalize_text(args.variant_id):
        return False
    return True


def _parse_offer_id_sample(value: Any) -> Dict[str, str]:
    offer_id = _normalize_text(value)
    if not offer_id:
        return {}
    parts = offer_id.split(":")
    if len(parts) < 4:
        return {"offer_id": offer_id}
    return {
        "offer_id": offer_id,
        "channel": parts[0],
        "merchant_id": parts[1],
        "product_id": parts[-2],
        "variant_id": parts[-1],
    }


def _report_summary(body: Dict[str, Any]) -> Dict[str, Any]:
    products = body.get("products") if isinstance(body.get("products"), list) else []
    return {
        "merchant_id": body.get("merchant_id"),
        "channel": body.get("channel"),
        "generated_at": body.get("generated_at"),
        "merchant_alpha_mode": body.get("merchant_alpha_mode"),
        "summary": {
            "product_count": len(products),
            "product_ids_sample": [
                _normalize_text(_json_dict(product).get("product_id"))
                for product in products[:5]
                if _normalize_text(_json_dict(product).get("product_id"))
            ],
        },
    }


def _fallback_offer_from_report(
    report_payload: Dict[str, Any],
    summary_export_payload: Dict[str, Any],
    args: argparse.Namespace,
    *,
    surface: str,
) -> Dict[str, Any]:
    products = report_payload.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("Full readiness report did not return any products for fallback offer selection.")

    sampled_offer: Dict[str, str] = {}
    if not (args.offer_id or args.product_id or args.variant_id):
        summary = _json_dict(summary_export_payload.get("summary"))
        for raw_offer_id in summary.get("offer_ids_sample") or []:
            parsed = _parse_offer_id_sample(raw_offer_id)
            if parsed:
                sampled_offer = parsed
                break

    target_offer_id = _normalize_text(args.offer_id) or _normalize_text(sampled_offer.get("offer_id"))
    target_product_id = _normalize_text(args.product_id) or _normalize_text(sampled_offer.get("product_id"))
    target_variant_id = _normalize_text(args.variant_id) or _normalize_text(sampled_offer.get("variant_id"))

    for raw_product in products:
        product = _json_dict(raw_product)
        product_id = _normalize_text(product.get("product_id"))
        if target_product_id and product_id != target_product_id:
            continue
        for raw_variant in product.get("variants") or []:
            variant = _json_dict(raw_variant)
            variant_id = _normalize_text(variant.get("variant_id"))
            if target_variant_id and variant_id != target_variant_id:
                continue
            coverage = _normalize_text(_json_dict(variant.get("channel_coverage")).get(surface)).lower()
            if coverage != "ready":
                continue
            offer = {
                "offer_id": target_offer_id or f"{surface}:{args.merchant_id}:{product_id}:{variant_id}",
                "product_id": product_id,
                "variant_id": variant_id,
                "title": _normalize_text(product.get("title")) or _normalize_text(variant.get("title")) or product_id,
                "variant_title": _normalize_text(variant.get("title")),
                "brand": _normalize_text(product.get("brand")),
                "category": _normalize_text(product.get("category")),
                "price": _json_dict(variant.get("price")),
            }
            if _offer_matches_args(offer, args):
                return offer
    raise ValueError("Readiness fallback report did not contain a ready offer matching the requested override.")


def _selected_offer(export_payload: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    offers = export_payload.get("offers")
    if not isinstance(offers, list) or not offers:
        raise ValueError("Readiness export did not return any ready offers.")

    for raw_offer in offers:
        if not isinstance(raw_offer, dict):
            continue
        offer = dict(raw_offer)
        if not _offer_matches_args(offer, args):
            continue
        return offer
    raise ValueError("No readiness export offer matched the requested override.")


def _offer_summary(offer: Dict[str, Any], *, title: str, unit_price: float, currency: str) -> Dict[str, Any]:
    return {
        "offer_id": offer.get("offer_id"),
        "product_id": offer.get("product_id"),
        "variant_id": offer.get("variant_id"),
        "brand": offer.get("brand"),
        "category": offer.get("category"),
        "title": title,
        "unit_price": unit_price,
        "currency": currency,
    }


def _parse_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value))
    except Exception:
        return None


def _pick_offer_fields(offer: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    title = _normalize_text(args.title) or _normalize_text(offer.get("title")) or _normalize_text(offer.get("variant_title")) or _normalize_text(offer.get("product_id"))
    unit_price = args.unit_price
    if unit_price is None:
        unit_price = _parse_money((_json_dict(offer.get("price")) or {}).get("amount"))
    if unit_price is None:
        raise ValueError("Ready offer did not provide a usable unit_price. Pass --unit-price explicitly.")
    currency = _normalize_text(args.currency) or _normalize_text((_json_dict(offer.get("price")) or {}).get("currency")) or "USD"
    return {
        "title": title,
        "unit_price": float(unit_price),
        "currency": currency,
        "brand": _normalize_text(args.brand) or _normalize_text(offer.get("brand")),
        "category": _normalize_text(args.category) or _normalize_text(offer.get("category")),
        "product_id": _normalize_text(offer.get("product_id")),
        "variant_id": _normalize_text(offer.get("variant_id")),
    }


def _resolve_token(redirect_url: str) -> str:
    token = (parse_qs(urlparse(redirect_url).query).get("token") or [""])[0]
    if not token:
        raise ValueError("redirectUrl did not contain token=")
    return token


def _parse_destination_pvt(destination_url: str) -> Dict[str, str]:
    qs = parse_qs(urlparse(destination_url).query)
    return {
        "pvt_click_id": (qs.get("pvt_click_id") or [""])[0],
        "pvt_surface": (qs.get("pvt_surface") or [""])[0],
        "pvt_product_id": (qs.get("pvt_product_id") or [""])[0],
        "pvt_variant_id": (qs.get("pvt_variant_id") or [""])[0],
        "pvt_prompt_cluster": (qs.get("pvt_prompt_cluster") or [""])[0],
    }


def _has_explicit_item_override(args: argparse.Namespace) -> bool:
    return bool(
        _normalize_text(args.product_id)
        and _normalize_text(args.variant_id)
        and args.unit_price is not None
    )


def _funnel_body_summary(body: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(body or {})
    return {
        "merchant_id": payload.get("merchant_id"),
        "surface": payload.get("surface"),
        "group_by": payload.get("group_by"),
        "summary": _json_dict(payload.get("summary")),
    }


def _summary_from_funnel(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary = _json_dict(payload.get("summary"))
    return {
        "indexed_exposure": int(summary.get("indexed_exposure") or 0),
        "surfaced_exposure": int(summary.get("surfaced_exposure") or 0),
        "clicked_exposure": int(summary.get("clicked_exposure") or 0),
        "clicked_events_total": int(summary.get("clicked_events_total") or 0),
        "ordered_conversion": int(summary.get("ordered_conversion") or 0),
        "refunded_orders": int(summary.get("refunded_orders") or 0),
        "refunded_amount": str(summary.get("refunded_amount") or "0"),
        "clicked_rate": summary.get("clicked_rate"),
        "ordered_rate": summary.get("ordered_rate"),
    }


def _delta_summary(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    delta: Dict[str, Any] = {}
    for key in ("indexed_exposure", "surfaced_exposure", "clicked_exposure", "clicked_events_total", "ordered_conversion", "refunded_orders"):
        delta[key] = int(after.get(key) or 0) - int(before.get(key) or 0)
    return delta


def _issues_for_interaction(issues_payload: Dict[str, Any], interaction_id: str) -> List[str]:
    matched_codes: List[str] = []
    for raw_issue in issues_payload.get("issues") or []:
        issue = _json_dict(raw_issue)
        code = _normalize_text(issue.get("code"))
        if code not in {"TRACE_BROKEN", "UNATTRIBUTED_ORDER"}:
            continue
        sample_ids = [str(item).strip() for item in (issue.get("sample_interaction_ids") or []) if str(item).strip()]
        if interaction_id in sample_ids:
            matched_codes.append(code)
    return matched_codes


def _trace_verdict(trace_payload: Dict[str, Any], *, interaction_id: str, click_id: str, order_id: str) -> Dict[str, Any]:
    interaction = _json_dict(trace_payload.get("interaction"))
    events = trace_payload.get("events") if isinstance(trace_payload.get("events"), list) else []
    event_types = [str(_json_dict(item).get("event_type") or "") for item in events if isinstance(item, dict)]
    payloads = [_json_dict(_json_dict(item).get("payload")) for item in events if isinstance(item, dict)]

    click_event_index = next((idx for idx, event_type in enumerate(event_types) if event_type == "surface.click"), None)
    impression_event_index = next((idx for idx, event_type in enumerate(event_types) if event_type == "surface.impression"), None)
    checkout_event_index = next((idx for idx, event_type in enumerate(event_types) if event_type == "checkout.created"), None)
    order_event_index = next((idx for idx, event_type in enumerate(event_types) if event_type == "order.created"), None)

    order_payloads = [payload for payload, event_type in zip(payloads, event_types) if event_type == "order.created"]
    click_payloads = [payload for payload, event_type in zip(payloads, event_types) if event_type in {"surface.click", "surface.impression"}]

    root_click_ok = _normalize_text(interaction.get("click_id")) == click_id or _normalize_text(_json_dict(interaction.get("metadata")).get("pvt_click_id")) == click_id
    click_payload_ok = any(
        _normalize_text(payload.get("click_id")) == click_id or _normalize_text(payload.get("pvt_click_id")) == click_id
        for payload in click_payloads
    )
    order_payload_ok = any(
        (
            _normalize_text(payload.get("click_id")) == click_id
            or _normalize_text(payload.get("pvt_click_id")) == click_id
        )
        and _normalize_text(payload.get("order_id")) == order_id
        for payload in order_payloads
    )
    ordered_sequence_ok = (
        checkout_event_index is not None
        and order_event_index is not None
        and (
            click_event_index is not None
            or impression_event_index is not None
            or root_click_ok
        )
    )
    required_events_ok = (
        "checkout.created" in set(event_types)
        and "order.created" in set(event_types)
        and (click_event_index is not None or impression_event_index is not None or root_click_ok)
    )
    interaction_ok = _normalize_text(interaction.get("interaction_id")) == interaction_id

    return {
        "ok": bool(interaction_ok and required_events_ok and (click_payload_ok or root_click_ok) and order_payload_ok and ordered_sequence_ok),
        "interaction_ok": interaction_ok,
        "required_events_ok": required_events_ok,
        "click_payload_ok": click_payload_ok,
        "root_click_ok": root_click_ok,
        "order_payload_ok": order_payload_ok,
        "ordered_sequence_ok": ordered_sequence_ok,
        "event_types": event_types,
    }


def _should_retry_checkout(step: Dict[str, Any]) -> bool:
    status_code = int(step.get("status_code") or 0)
    if status_code in {429, 502, 503, 504}:
        return True

    body = _json_dict(step.get("body"))
    error = _json_dict(body.get("error"))
    detail = _json_dict(body.get("detail"))
    retryable_markers = {
        "temporary_unavailable",
        "database_busy",
        "db_busy",
    }
    observed = {
        _normalize_text(error.get("code")).lower(),
        _normalize_text(error.get("message")).lower(),
        _normalize_text(detail.get("error")).lower(),
        _normalize_text(detail.get("message")).lower(),
        _normalize_text(body.get("detail")).lower(),
    }
    return any(marker in observed for marker in retryable_markers)


def _render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    identifiers = report.get("identifiers") or {}
    selected_offer = report.get("selected_offer") or {}
    funnel = report.get("funnel") or {}
    delta = funnel.get("delta") or {}
    lines = [
        "# Real Click Order Funnel Signoff",
        "",
        f"- base_url: `{report.get('base_url')}`",
        f"- merchant_id: `{report.get('merchant_id')}`",
        f"- surface: `{report.get('surface')}`",
        f"- analytics_surface: `{report.get('analytics_surface')}`",
        f"- overall_ok: `{report.get('overall_ok')}`",
        "",
        "## Selected Offer",
        "",
        f"- offer_id: `{selected_offer.get('offer_id')}`",
        f"- product_id: `{selected_offer.get('product_id')}`",
        f"- variant_id: `{selected_offer.get('variant_id')}`",
        f"- title: `{selected_offer.get('title')}`",
        f"- unit_price: `{selected_offer.get('unit_price')}` `{selected_offer.get('currency')}`",
        "",
        "## Identifiers",
        "",
        f"- click_id: `{identifiers.get('click_id')}`",
        f"- interaction_id: `{identifiers.get('interaction_id')}`",
        f"- checkout_id: `{identifiers.get('checkout_id')}`",
        f"- order_id: `{identifiers.get('order_id')}`",
        "",
        "## Funnel",
        "",
        f"- baseline.clicked_exposure: `{(funnel.get('baseline') or {}).get('clicked_exposure')}`",
        f"- baseline.ordered_conversion: `{(funnel.get('baseline') or {}).get('ordered_conversion')}`",
        f"- post.clicked_exposure: `{(funnel.get('post') or {}).get('clicked_exposure')}`",
        f"- post.ordered_conversion: `{(funnel.get('post') or {}).get('ordered_conversion')}`",
        f"- delta.clicked_exposure: `{delta.get('clicked_exposure')}`",
        f"- delta.ordered_conversion: `{delta.get('ordered_conversion')}`",
        f"- refunded_orders: `{(funnel.get('post') or {}).get('refunded_orders')}`",
        "",
        "## Verdict",
        "",
        f"- health_ok: `{summary.get('health_ok')}`",
        f"- ready_offer_ok: `{summary.get('ready_offer_ok')}`",
        f"- resolve_ok: `{summary.get('resolve_ok')}`",
        f"- click_ok: `{summary.get('click_ok')}`",
        f"- order_ok: `{summary.get('order_ok')}`",
        f"- issues_ok: `{summary.get('issues_ok')}`",
        f"- trace_ok: `{summary.get('trace_ok')}`",
        "",
        "## Steps",
        "",
    ]
    for step in report.get("steps") or []:
        lines.append(
            f"- `{step.get('step')}` status=`{step.get('status_code')}` elapsed_ms=`{step.get('elapsed_ms')}` ok=`{step.get('ok')}`"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    internal_headers, agent_headers, merchant_headers = _ensure_auth_headers(args)
    base_url = str(args.base_url).rstrip("/")
    surface = _normalize_text(args.surface).lower() or DEFAULT_SURFACE
    tool = _normalize_text(args.tool).lower() or surface
    analytics_surface = _normalize_text(args.analytics_surface).lower() or None

    steps: List[Dict[str, Any]] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "pivota-click-order-funnel-signoff/1.0"})
    resolve_ok = False
    click_ok = False
    order_ok = False
    explicit_item_override = _has_explicit_item_override(args)
    offer_selection_ok = False

    health_step = _request_step(
        session=session,
        steps=steps,
        name="health",
        method="GET",
        url=f"{base_url}/health",
        timeout=args.timeout_seconds,
        ok_if=lambda status, body: status == 200 and _normalize_text(body.get("status")) in {"ok", "healthy"},
    )

    readiness_state_step = _request_step(
        session=session,
        steps=steps,
        name="merchant_readiness_state",
        method="GET",
        url=f"{base_url}/merchant/analytics/readiness-state",
        headers=merchant_headers,
        timeout=args.timeout_seconds,
        ok_if=lambda status, body: status == 200 and _normalize_text(body.get("merchant_id")) == _normalize_text(args.merchant_id),
    )

    readiness_report_step = _request_step(
        session=session,
        steps=steps,
        name="readiness_report_summary",
        method="GET",
        url=f"{base_url}/internal/readiness/merchants/{args.merchant_id}/report?channel={surface}&summary_only=true&sample_limit=25",
        headers=internal_headers,
        timeout=args.timeout_seconds,
        ok_if=lambda status, body: status == 200 and _normalize_text(body.get("merchant_id")) == _normalize_text(args.merchant_id),
    )

    if explicit_item_override:
        export_body_raw = {}
        export_summary_body_raw: Dict[str, Any] = {}
        report_full_body_raw: Dict[str, Any] = {}
        export_step = _record_step(
            steps=steps,
            name="readiness_export_full",
            ok=True,
            elapsed_ms=0.0,
            status_code=None,
            body={"skipped": True, "reason": "explicit_item_override"},
        )
    else:
        export_summary_body_raw = {}
        report_full_body_raw = {}
        export_started = time.perf_counter()
        try:
            export_response = session.request(
                "GET",
                f"{base_url}/internal/readiness/merchants/{args.merchant_id}/exports/{surface}",
                headers=internal_headers,
                timeout=args.timeout_seconds,
            )
            export_body_raw = _json_body(export_response)
            export_offers = export_body_raw.get("offers") if isinstance(export_body_raw.get("offers"), list) else []
            export_step = _record_step(
                steps=steps,
                name="readiness_export_full",
                ok=export_response.status_code == 200 and len(export_offers) > 0,
                elapsed_ms=(time.perf_counter() - export_started) * 1000.0,
                status_code=export_response.status_code,
                body={
                    "merchant_id": export_body_raw.get("merchant_id"),
                    "channel": export_body_raw.get("channel"),
                    "generated_at": export_body_raw.get("generated_at"),
                    "merchant_alpha_mode": export_body_raw.get("merchant_alpha_mode"),
                    "summary": {
                        "offer_count": len(export_offers),
                        "offer_ids_sample": [
                            _normalize_text(_json_dict(item).get("offer_id"))
                            for item in export_offers[:5]
                            if _normalize_text(_json_dict(item).get("offer_id"))
                        ],
                        "product_ids_sample": [
                            _normalize_text(_json_dict(item).get("product_id"))
                            for item in export_offers[:5]
                            if _normalize_text(_json_dict(item).get("product_id"))
                        ],
                    },
                },
            )
        except Exception as exc:
            export_body_raw = {}
            export_step = _record_step(
                steps=steps,
                name="readiness_export_full",
                ok=False,
                elapsed_ms=(time.perf_counter() - export_started) * 1000.0,
                status_code=None,
                body={"error": str(exc)},
            )

        if not export_step.get("ok"):
            export_summary_started = time.perf_counter()
            try:
                export_summary_response = session.request(
                    "GET",
                    f"{base_url}/internal/readiness/merchants/{args.merchant_id}/exports/{surface}?summary_only=true&sample_limit=25",
                    headers=internal_headers,
                    timeout=args.timeout_seconds,
                )
                export_summary_body_raw = _json_body(export_summary_response)
                export_summary = _json_dict(export_summary_body_raw.get("summary"))
                export_summary_step = _record_step(
                    steps=steps,
                    name="readiness_export_summary_fallback",
                    ok=export_summary_response.status_code == 200 and int(export_summary.get("offer_count") or 0) > 0,
                    elapsed_ms=(time.perf_counter() - export_summary_started) * 1000.0,
                    status_code=export_summary_response.status_code,
                    body={
                        "merchant_id": export_summary_body_raw.get("merchant_id"),
                        "channel": export_summary_body_raw.get("channel"),
                        "generated_at": export_summary_body_raw.get("generated_at"),
                        "merchant_alpha_mode": export_summary_body_raw.get("merchant_alpha_mode"),
                        "summary": {
                            "offer_count": int(export_summary.get("offer_count") or 0),
                            "offer_ids_sample": export_summary.get("offer_ids_sample") or [],
                            "product_ids_sample": export_summary.get("product_ids_sample") or [],
                        },
                    },
                )
            except Exception as exc:
                export_summary_step = _record_step(
                    steps=steps,
                    name="readiness_export_summary_fallback",
                    ok=False,
                    elapsed_ms=(time.perf_counter() - export_summary_started) * 1000.0,
                    status_code=None,
                    body={"error": str(exc)},
                )

            if export_summary_step.get("ok"):
                report_full_started = time.perf_counter()
                try:
                    report_full_response = session.request(
                        "GET",
                        f"{base_url}/internal/readiness/merchants/{args.merchant_id}/report?channel={surface}&summary_only=false",
                        headers=internal_headers,
                        timeout=max(float(args.timeout_seconds), 60.0),
                    )
                    report_full_body_raw = _json_body(report_full_response)
                    report_full_step = _record_step(
                        steps=steps,
                        name="readiness_report_full_fallback",
                        ok=report_full_response.status_code == 200 and isinstance(report_full_body_raw.get("products"), list),
                        elapsed_ms=(time.perf_counter() - report_full_started) * 1000.0,
                        status_code=report_full_response.status_code,
                        body=_report_summary(report_full_body_raw),
                    )
                except Exception as exc:
                    report_full_step = _record_step(
                        steps=steps,
                        name="readiness_report_full_fallback",
                        ok=False,
                        elapsed_ms=(time.perf_counter() - report_full_started) * 1000.0,
                        status_code=None,
                        body={"error": str(exc)},
                    )

                if report_full_step.get("ok"):
                    try:
                        export_body_raw = {
                            "merchant_id": args.merchant_id,
                            "channel": surface,
                            "merchant_alpha_mode": report_full_body_raw.get("merchant_alpha_mode"),
                            "offers": [
                                _fallback_offer_from_report(
                                    report_full_body_raw,
                                    export_summary_body_raw,
                                    args,
                                    surface=surface,
                                )
                            ],
                        }
                    except Exception as exc:
                        _record_step(
                            steps=steps,
                            name="readiness_offer_fallback_selection",
                            ok=False,
                            elapsed_ms=0.0,
                            status_code=None,
                            body={"error": str(exc)},
                        )

    baseline_funnel_step = _request_step(
        session=session,
        steps=steps,
        name="merchant_commerce_funnel_baseline",
        method="GET",
        url=_funnel_query(base_url, analytics_surface),
        headers=merchant_headers,
        timeout=args.timeout_seconds,
        ok_if=lambda status, body: status == 200 and isinstance(body.get("summary"), dict),
    )
    baseline_funnel_step["body"] = _funnel_body_summary(baseline_funnel_step.get("body") or {})

    overall_ok = False
    selected_offer_summary: Dict[str, Any] = {}
    identifiers: Dict[str, Any] = {
        "click_id": None,
        "interaction_id": None,
        "checkout_id": None,
        "order_id": None,
        "payment_action": None,
    }
    funnel_baseline = _summary_from_funnel(baseline_funnel_step.get("body") or {})
    funnel_post: Dict[str, Any] = {}
    funnel_delta: Dict[str, Any] = {}

    try:
        readiness_state = readiness_state_step.get("body") or {}
        platform = _normalize_text(readiness_state.get("primary_platform")).lower() or "shopify"
        if explicit_item_override:
            offer_fields = {
                "title": _normalize_text(args.title) or _normalize_text(args.product_id),
                "unit_price": float(args.unit_price),
                "currency": _normalize_text(args.currency) or "USD",
                "brand": _normalize_text(args.brand),
                "category": _normalize_text(args.category),
                "product_id": _normalize_text(args.product_id),
                "variant_id": _normalize_text(args.variant_id),
            }
            product_id = offer_fields["product_id"]
            variant_id = offer_fields["variant_id"]
            canonical_product_id = _normalize_text(args.canonical_product_id) or _canonical_product_id(args.merchant_id, platform, product_id)
            canonical_variant_id = _normalize_text(args.canonical_variant_id) or _canonical_variant_id(args.merchant_id, platform, product_id, variant_id)
            selected_offer_summary = {
                "offer_id": None,
                "product_id": product_id,
                "variant_id": variant_id,
                "brand": offer_fields["brand"] or None,
                "category": offer_fields["category"] or None,
                "title": offer_fields["title"],
                "unit_price": offer_fields["unit_price"],
                "currency": offer_fields["currency"],
                "selected_via": "explicit_override",
            }
        else:
            export_payload = export_body_raw
            offer = _selected_offer(export_payload, args)
            offer_fields = _pick_offer_fields(offer, args)
            selected_offer_summary = _offer_summary(
                offer,
                title=offer_fields["title"],
                unit_price=offer_fields["unit_price"],
                currency=offer_fields["currency"],
            )
            selected_offer_summary["selected_via"] = (
                "summary_export_fallback"
                if not export_step.get("ok")
                else "full_export"
            )
            product_id = offer_fields["product_id"]
            variant_id = offer_fields["variant_id"]
            if not product_id or not variant_id:
                raise ValueError("Selected readiness offer did not include product_id and variant_id.")
            canonical_product_id = _normalize_text(args.canonical_product_id) or _canonical_product_id(args.merchant_id, platform, product_id)
            canonical_variant_id = _normalize_text(args.canonical_variant_id) or _canonical_variant_id(args.merchant_id, platform, product_id, variant_id)
        selected_offer_summary["canonical_product_id"] = canonical_product_id
        selected_offer_summary["canonical_variant_id"] = canonical_variant_id
        selected_offer_summary["platform"] = platform
        offer_selection_ok = bool(product_id and variant_id)
        resolve_sku_id = _normalize_text(args.sku_id) or _normalize_text(variant_id)

        resolve_payload = {
            "market": args.market,
            "tool": tool,
            "candidates": {
                **({"skuId": resolve_sku_id} if resolve_sku_id else {}),
                **({"brand": offer_fields["brand"]} if offer_fields["brand"] else {}),
                **({"category": offer_fields["category"]} if offer_fields["category"] else {}),
            },
            "context": {
                "merchantId": args.merchant_id,
                "surface": surface,
                "platform": platform,
                "platform_product_id": product_id,
                "platform_variant_id": variant_id,
                "pvt_product_id": canonical_product_id,
                "pvt_variant_id": canonical_variant_id,
                **({"promptCluster": args.prompt_cluster} if _normalize_text(args.prompt_cluster) else {}),
            },
        }
        resolve_step = _request_step(
            session=session,
            steps=steps,
            name="links_resolve",
            method="POST",
            url=f"{base_url}/api/links/resolve",
            timeout=args.timeout_seconds,
            json=resolve_payload,
            ok_if=lambda status, body: status == 200 and bool(body.get("matched")) and isinstance((_json_dict(body.get("resolved"))).get("redirectUrl"), str),
        )
        resolve_ok = bool(resolve_step.get("ok"))
        resolved = _json_dict((resolve_step.get("body") or {}).get("resolved"))
        destination_url = _normalize_text(resolved.get("destinationUrl"))
        redirect_url = _normalize_text(resolved.get("redirectUrl"))
        if not destination_url or not redirect_url:
            raise ValueError("Link resolve did not return destinationUrl/redirectUrl.")
        pvt_from_destination = _parse_destination_pvt(destination_url)
        click_id = _normalize_text(pvt_from_destination.get("pvt_click_id"))
        if not click_id:
            raise ValueError("Resolved destinationUrl did not carry pvt_click_id.")
        if _normalize_text(pvt_from_destination.get("pvt_surface")).lower() != surface:
            raise ValueError("Resolved destinationUrl did not preserve the requested surface.")
        if _normalize_text(pvt_from_destination.get("pvt_product_id")) != canonical_product_id:
            raise ValueError("Resolved destinationUrl did not carry the expected pvt_product_id.")
        if _normalize_text(pvt_from_destination.get("pvt_variant_id")) != canonical_variant_id:
            raise ValueError("Resolved destinationUrl did not carry the expected pvt_variant_id.")
        token = _resolve_token(redirect_url)
        interaction_id = _interaction_id_from_click(args.merchant_id, click_id)

        identifiers["click_id"] = click_id
        identifiers["interaction_id"] = interaction_id

        if args.skip_impression:
            _record_step(
                steps=steps,
                name="links_impression",
                ok=True,
                elapsed_ms=0.0,
                status_code=None,
                body={"skipped": True},
            )
        else:
            _request_step(
                session=session,
                steps=steps,
                name="links_impression",
                method="POST",
                url=f"{base_url}/api/links/impression",
                timeout=args.timeout_seconds,
                json={"token": token, "context": {"merchantId": args.merchant_id, "surface": surface}},
                ok_if=lambda status, body: status == 200 and bool(body.get("ok")),
            )

        click_started = time.perf_counter()
        click_response = session.request(
            "GET",
            redirect_url,
            allow_redirects=False,
            timeout=args.timeout_seconds,
        )
        click_step = _record_step(
            steps=steps,
            name="redirect_click",
            ok=click_response.status_code in {301, 302, 303, 307, 308},
            elapsed_ms=(time.perf_counter() - click_started) * 1000.0,
            status_code=click_response.status_code,
            body={
                "redirect_url": redirect_url,
                "location": click_response.headers.get("location"),
            },
        )
        click_ok = bool(click_step.get("ok"))

        checkout_payload = {
            "merchant_id": args.merchant_id,
            "interaction_id": interaction_id,
            "customer_email": args.buyer_email,
            "customer_name": args.customer_name,
            "source": surface,
            "currency": offer_fields["currency"],
            "preferred_psp": args.preferred_psp,
            "idempotency_key": f"{args.run_id}:click-order",
            "shipping_address": {
                "name": args.address_name,
                "address_line1": args.address_line1,
                "address_line2": args.address_line2 or None,
                "city": args.city,
                "state": args.state or None,
                "postal_code": args.postal_code,
                "country": args.country,
                "phone": args.phone or None,
            },
            "items": [
                {
                    "product_id": product_id,
                    "variant_id": variant_id,
                    "quantity": args.quantity,
                    "title": offer_fields["title"],
                    "unit_price": offer_fields["unit_price"],
                    "currency": offer_fields["currency"],
                }
            ],
            "metadata": {
                "pvt_click_id": click_id,
                "pvt_surface": pvt_from_destination["pvt_surface"],
                "pvt_product_id": canonical_product_id,
                "pvt_variant_id": canonical_variant_id,
                "platform": platform,
                "platform_product_id": product_id,
                "platform_variant_id": variant_id,
                "trace_id": f"ops_click_order:{args.run_id}",
                "brief_id": f"ops_click_order:{args.run_id}",
                **({"pvt_prompt_cluster": pvt_from_destination["pvt_prompt_cluster"]} if _normalize_text(pvt_from_destination.get("pvt_prompt_cluster")) else {}),
            },
        }
        checkout_step: Dict[str, Any] = {}
        max_checkout_attempts = 3
        for attempt in range(1, max_checkout_attempts + 1):
            checkout_attempt_step = _request_step(
                session=session,
                steps=steps,
                name=f"commerce_checkout_create_attempt_{attempt}",
                method="POST",
                url=f"{base_url}/agent/v2/commerce/checkouts",
                headers=agent_headers,
                timeout=args.timeout_seconds,
                json=checkout_payload,
                ok_if=lambda status, body: status == 200 and _normalize_text(body.get("checkout_id")) != "",
            )
            should_retry_checkout = (
                attempt < max_checkout_attempts
                and not checkout_attempt_step.get("ok")
                and _should_retry_checkout(checkout_attempt_step)
            )
            if should_retry_checkout:
                time.sleep(float(attempt))
                continue
            checkout_step = _record_step(
                steps=steps,
                name="commerce_checkout_create",
                ok=bool(checkout_attempt_step.get("ok")),
                elapsed_ms=float(checkout_attempt_step.get("elapsed_ms") or 0.0),
                status_code=checkout_attempt_step.get("status_code"),
                body=checkout_attempt_step.get("body") or {},
                extra={"attempt": attempt},
            )
            break
        order_ok = bool(checkout_step.get("ok"))
        checkout_body = checkout_step.get("body") or {}
        checkout_id = _normalize_text(checkout_body.get("checkout_id"))
        order_id = _normalize_text(checkout_body.get("order_id")) or checkout_id
        payment_action = _json_dict(checkout_body.get("payment_action"))
        if not checkout_id or not order_id:
            raise ValueError("Checkout create did not return checkout_id/order_id.")
        identifiers["checkout_id"] = checkout_id
        identifiers["order_id"] = order_id
        identifiers["payment_action"] = _redact_sensitive(payment_action)

        _request_step(
            session=session,
            steps=steps,
            name="commerce_checkout_status",
            method="GET",
            url=f"{base_url}/agent/v2/commerce/checkouts/{checkout_id}/status",
            headers=agent_headers,
            timeout=args.timeout_seconds,
            ok_if=lambda status, body: status == 200 and _normalize_text(body.get("checkout_id")) == checkout_id,
        )

        deadline = time.monotonic() + max(0.0, float(args.poll_timeout_seconds))
        latest_funnel_step: Optional[Dict[str, Any]] = None
        latest_trace_step: Optional[Dict[str, Any]] = None
        trace_verdict: Optional[Dict[str, Any]] = None
        while True:
            latest_funnel_step = _request_step(
                session=session,
                steps=[],
                name="merchant_commerce_funnel_post_poll",
                method="GET",
                url=_funnel_query(base_url, analytics_surface),
                headers=merchant_headers,
                timeout=args.timeout_seconds,
                ok_if=lambda status, body: status == 200 and isinstance(body.get("summary"), dict),
            )
            funnel_post = _summary_from_funnel(latest_funnel_step.get("body") or {})
            funnel_delta = _delta_summary(funnel_baseline, funnel_post)

            latest_trace_step = _request_step(
                session=session,
                steps=[],
                name="merchant_commerce_interaction_trace_poll",
                method="GET",
                url=_trace_query(base_url, interaction_id),
                headers=merchant_headers,
                timeout=args.timeout_seconds,
                ok_if=lambda status, body: status == 200 and _normalize_text((_json_dict(body.get("interaction"))).get("interaction_id")) == interaction_id,
            )
            trace_verdict = _trace_verdict(
                latest_trace_step.get("body") or {},
                interaction_id=interaction_id,
                click_id=click_id,
                order_id=order_id,
            )

            funnel_ok = (
                funnel_post["clicked_exposure"] >= funnel_baseline["clicked_exposure"] + 1
                and funnel_post["ordered_conversion"] >= funnel_baseline["ordered_conversion"] + 1
                and funnel_post["refunded_orders"] == 0
            )
            if funnel_ok and trace_verdict["ok"]:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.0, float(args.poll_interval_seconds)))

        _record_step(
            steps=steps,
            name="merchant_commerce_funnel_post",
            ok=bool(latest_funnel_step and latest_funnel_step.get("ok")),
            elapsed_ms=float((latest_funnel_step or {}).get("elapsed_ms") or 0.0),
            status_code=(latest_funnel_step or {}).get("status_code"),
            body=_funnel_body_summary((latest_funnel_step or {}).get("body") or {}),
        )

        issues_step = _request_step(
            session=session,
            steps=steps,
            name="merchant_commerce_funnel_issues_post",
            method="GET",
            url=_issues_query(base_url, analytics_surface),
            headers=merchant_headers,
            timeout=args.timeout_seconds,
            ok_if=lambda status, body: status == 200 and isinstance(body.get("issues"), list),
        )

        _record_step(
            steps=steps,
            name="merchant_commerce_interaction_trace",
            ok=bool(latest_trace_step and latest_trace_step.get("ok")),
            elapsed_ms=float((latest_trace_step or {}).get("elapsed_ms") or 0.0),
            status_code=(latest_trace_step or {}).get("status_code"),
            body=(latest_trace_step or {}).get("body") or {},
            extra={"trace_verdict": trace_verdict or {}},
        )

        critical_issue_codes = _issues_for_interaction(issues_step.get("body") or {}, interaction_id)
        summary = {
            "health_ok": bool(health_step.get("ok")),
            "ready_offer_ok": bool(
                readiness_state_step.get("ok")
                and readiness_report_step.get("ok")
                and offer_selection_ok
            ),
            "resolve_ok": resolve_ok,
            "click_ok": click_ok,
            "order_ok": order_ok,
            "issues_ok": len(critical_issue_codes) == 0,
            "trace_ok": bool((trace_verdict or {}).get("ok")),
            "critical_issue_codes_for_interaction": critical_issue_codes,
            "underlying_platform": platform,
            "baseline_funnel_ok": bool(baseline_funnel_step.get("ok")),
            "post_funnel_ok": bool(latest_funnel_step and latest_funnel_step.get("ok")),
        }
        overall_ok = bool(
            summary["health_ok"]
            and summary["ready_offer_ok"]
            and summary["resolve_ok"]
            and summary["click_ok"]
            and summary["order_ok"]
            and summary["issues_ok"]
            and summary["trace_ok"]
            and summary["baseline_funnel_ok"]
            and summary["post_funnel_ok"]
            and funnel_post["clicked_exposure"] >= funnel_baseline["clicked_exposure"] + 1
            and funnel_post["ordered_conversion"] >= funnel_baseline["ordered_conversion"] + 1
            and funnel_post["refunded_orders"] == 0
        )

        report = {
            "base_url": base_url,
            "merchant_id": args.merchant_id,
            "surface": surface,
            "analytics_surface": analytics_surface,
            "market": args.market,
            "tool": tool,
            "run_id": args.run_id,
            "overall_ok": overall_ok,
            "selected_offer": selected_offer_summary,
            "identifiers": identifiers,
            "funnel": {
                "baseline": funnel_baseline,
                "post": funnel_post,
                "delta": funnel_delta,
            },
            "summary": summary,
            "steps": steps,
        }
        _write_if_requested(args.output_json, json.dumps(_redact_sensitive(report), indent=2, sort_keys=True))
        _write_if_requested(args.output_md, _render_markdown(_redact_sensitive(report)))
        return 0 if overall_ok else 1
    except Exception as exc:
        failure_summary = {
            "health_ok": bool(health_step.get("ok")),
            "ready_offer_ok": bool(
                readiness_state_step.get("ok")
                and readiness_report_step.get("ok")
                and offer_selection_ok
            ),
            "resolve_ok": resolve_ok,
            "click_ok": click_ok,
            "order_ok": order_ok,
            "issues_ok": False,
            "trace_ok": False,
            "error": str(exc),
            "baseline_funnel_ok": bool(baseline_funnel_step.get("ok")),
        }
        report = {
            "base_url": base_url,
            "merchant_id": args.merchant_id,
            "surface": surface,
            "analytics_surface": analytics_surface,
            "market": args.market,
            "tool": tool,
            "run_id": args.run_id,
            "overall_ok": False,
            "selected_offer": selected_offer_summary,
            "identifiers": identifiers,
            "funnel": {
                "baseline": funnel_baseline,
                "post": funnel_post,
                "delta": funnel_delta,
            },
            "summary": failure_summary,
            "steps": steps + [
                {
                    "step": "fatal_error",
                    "ok": False,
                    "elapsed_ms": 0.0,
                    "status_code": None,
                    "body": {"error": str(exc)},
                }
            ],
        }
        _write_if_requested(args.output_json, json.dumps(_redact_sensitive(report), indent=2, sort_keys=True))
        _write_if_requested(args.output_md, _render_markdown(_redact_sensitive(report)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
