from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2024-01"


class ShopifyTransactionSyncError(Exception):
    pass


class ShopifyTransactionSyncAuthError(ShopifyTransactionSyncError):
    pass


class ShopifyTransactionSyncRateLimitError(ShopifyTransactionSyncError):
    pass


def extract_shopify_access_token(api_key_raw: Any) -> Optional[str]:
    """
    Support both legacy raw-string tokens and JSON-wrapped tokens:
    { "access_token": "...", "scope": "...", ... }.
    """
    if not api_key_raw:
        return None
    if isinstance(api_key_raw, str) and api_key_raw.strip().startswith("{"):
        try:
            data = json.loads(api_key_raw)
            if isinstance(data, dict):
                token = data.get("access_token") or data.get("token")
                return str(token).strip() if token else None
        except Exception:
            return None
    if isinstance(api_key_raw, str):
        return api_key_raw.strip() or None
    return None


def normalize_shopify_gateway(psp_used: Optional[str]) -> Optional[str]:
    if not psp_used:
        return None
    s = str(psp_used).strip().lower()
    return s or None


async def _shopify_request(
    *,
    method: str,
    url: str,
    access_token: str,
    json_body: Optional[Dict[str, Any]] = None,
    timeout_s: float = 12.0,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.request(
            method,
            url,
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json=json_body,
        )
    if resp.status_code == 429:
        raise ShopifyTransactionSyncRateLimitError("Shopify rate limit exceeded (status=429)")
    if resp.status_code in (401, 403):
        raise ShopifyTransactionSyncAuthError(f"Shopify auth failed (status={resp.status_code})")
    return resp


def _safe_text(resp: httpx.Response, limit: int = 800) -> str:
    try:
        return (resp.text or "")[:limit]
    except Exception:
        return ""


def _merge_note_attributes(
    existing: Any,
    updates: Dict[str, str],
) -> List[Dict[str, str]]:
    current: List[Dict[str, str]] = []
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict) and "name" in item and "value" in item:
                current.append(
                    {"name": str(item["name"]), "value": str(item["value"])}
                )

    by_name = {a["name"]: a for a in current if a.get("name")}
    for k, v in updates.items():
        key = str(k)
        val = str(v)
        if not key:
            continue
        by_name[key] = {"name": key, "value": val}
    return list(by_name.values())


async def annotate_shopify_order_best_effort(
    *,
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    api_version: str = DEFAULT_API_VERSION,
    note_attributes: Optional[Dict[str, str]] = None,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Best-effort order annotation for reconciliation when Shopify rejects transaction creation.
    Avoid writing any PII; only store PSP refs + internal order identifiers.
    """
    note_attributes = note_attributes or {}
    tags = tags or []

    get_url = (
        f"https://{shop_domain}/admin/api/{api_version}/orders/{shopify_order_id}.json"
        "?fields=id,note_attributes,tags"
    )
    resp = await _shopify_request(method="GET", url=get_url, access_token=access_token)
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code, "error": _safe_text(resp)}

    data = resp.json() if resp.content else {}
    order = (data or {}).get("order") if isinstance(data, dict) else None
    if not isinstance(order, dict):
        return {"ok": False, "status": 500, "error": "Unexpected Shopify order response"}

    existing_attrs = order.get("note_attributes")
    merged_attrs = _merge_note_attributes(existing_attrs, note_attributes)

    existing_tags = str(order.get("tags") or "")
    next_tags = [t.strip() for t in existing_tags.split(",") if t.strip()]
    for t in tags:
        tt = str(t).strip()
        if tt and tt not in next_tags:
            next_tags.append(tt)

    put_url = f"https://{shop_domain}/admin/api/{api_version}/orders/{shopify_order_id}.json"
    put_body = {
        "order": {
            "id": int(shopify_order_id) if str(shopify_order_id).isdigit() else shopify_order_id,
            "note_attributes": merged_attrs,
            "tags": ", ".join(next_tags),
        }
    }
    put_resp = await _shopify_request(
        method="PUT", url=put_url, access_token=access_token, json_body=put_body
    )
    return {
        "ok": put_resp.status_code in (200, 201),
        "status": put_resp.status_code,
        "error": None if put_resp.status_code in (200, 201) else _safe_text(put_resp),
    }


async def list_shopify_order_transactions(
    *,
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    api_version: str = DEFAULT_API_VERSION,
) -> List[Dict[str, Any]]:
    url = f"https://{shop_domain}/admin/api/{api_version}/orders/{shopify_order_id}/transactions.json"
    resp = await _shopify_request(method="GET", url=url, access_token=access_token)
    if resp.status_code != 200:
        raise ShopifyTransactionSyncError(
            f"Failed to list transactions (status={resp.status_code})"
        )
    data = resp.json() if resp.content else {}
    txns = (data or {}).get("transactions") if isinstance(data, dict) else None
    return txns if isinstance(txns, list) else []


async def create_shopify_order_transaction(
    *,
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    transaction: Dict[str, Any],
    api_version: str = DEFAULT_API_VERSION,
) -> Dict[str, Any]:
    url = f"https://{shop_domain}/admin/api/{api_version}/orders/{shopify_order_id}/transactions.json"
    body = {"transaction": transaction}
    resp = await _shopify_request(method="POST", url=url, access_token=access_token, json_body=body)
    if resp.status_code not in (200, 201):
        raise ShopifyTransactionSyncError(
            f"Failed to create transaction (status={resp.status_code}) { _safe_text(resp) }"
        )
    data = resp.json() if resp.content else {}
    txn = (data or {}).get("transaction") if isinstance(data, dict) else None
    return txn if isinstance(txn, dict) else {}


async def ensure_external_payment_transaction_best_effort(
    *,
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    psp_used: Optional[str],
    external_payment_ref: Optional[str],
    amount: float,
    currency: str,
    api_version: str = DEFAULT_API_VERSION,
    pivota_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    gateway = normalize_shopify_gateway(psp_used)
    payment_ref = str(external_payment_ref or "").strip() or None
    if not gateway or not payment_ref:
        return {"ok": False, "skipped": True, "reason": "missing_gateway_or_payment_ref"}

    try:
        txns = await list_shopify_order_transactions(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            api_version=api_version,
        )
    except Exception as e:
        txns = []
        logger.warning(
            "shopify_txn.list_failed order_id=%s shopify_order_id=%s err=%s",
            pivota_order_id,
            shopify_order_id,
            str(e),
        )

    for t in txns:
        if not isinstance(t, dict):
            continue
        if str(t.get("gateway") or "").lower() != gateway:
            continue
        if str(t.get("authorization") or "").strip() == payment_ref:
            return {"ok": True, "created": False, "transaction_id": t.get("id")}

    try:
        txn = await create_shopify_order_transaction(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            api_version=api_version,
            transaction={
                "kind": "sale",
                "status": "success",
                "amount": str(amount),
                "currency": str(currency or "USD"),
                "gateway": gateway,
                "authorization": payment_ref,
                "source_name": "external",
            },
        )
        return {"ok": True, "created": True, "transaction_id": txn.get("id")}
    except Exception as e:
        logger.warning(
            "shopify_txn.create_failed order_id=%s shopify_order_id=%s gateway=%s err=%s",
            pivota_order_id,
            shopify_order_id,
            gateway,
            str(e),
        )
        annotation = await annotate_shopify_order_best_effort(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            api_version=api_version,
            note_attributes={
                "pivota_order_id": str(pivota_order_id or ""),
                "pivota_psp": gateway,
                "pivota_psp_payment_ref": payment_ref,
            },
            tags=["pivota-external-psp"],
        )
        return {"ok": False, "created": False, "error": str(e), "annotation": annotation}


async def ensure_external_refund_transaction_best_effort(
    *,
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    psp_used: Optional[str],
    external_refund_ref: Optional[str],
    amount: float,
    currency: str,
    api_version: str = DEFAULT_API_VERSION,
    pivota_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    gateway = normalize_shopify_gateway(psp_used)
    refund_ref = str(external_refund_ref or "").strip() or None
    if not gateway or not refund_ref:
        return {"ok": False, "skipped": True, "reason": "missing_gateway_or_refund_ref"}

    txns: List[Dict[str, Any]] = []
    try:
        txns = await list_shopify_order_transactions(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            api_version=api_version,
        )
    except Exception as e:
        logger.warning(
            "shopify_refund_txn.list_failed order_id=%s shopify_order_id=%s err=%s",
            pivota_order_id,
            shopify_order_id,
            str(e),
        )

    for t in txns:
        if not isinstance(t, dict):
            continue
        if str(t.get("gateway") or "").lower() != gateway:
            continue
        if str(t.get("authorization") or "").strip() == refund_ref:
            return {"ok": True, "created": False, "transaction_id": t.get("id")}

    parent_id: Optional[int] = None
    for t in reversed(txns):
        if not isinstance(t, dict):
            continue
        if str(t.get("gateway") or "").lower() != gateway:
            continue
        kind = str(t.get("kind") or "").lower()
        status = str(t.get("status") or "").lower()
        if kind in ("sale", "capture") and status == "success":
            tid = t.get("id")
            if isinstance(tid, int):
                parent_id = tid
            else:
                try:
                    parent_id = int(str(tid))
                except Exception:
                    parent_id = None
            if parent_id is not None:
                break

    try:
        txn_payload: Dict[str, Any] = {
            "kind": "refund",
            "status": "success",
            "amount": str(amount),
            "currency": str(currency or "USD"),
            "gateway": gateway,
            "authorization": refund_ref,
            "source_name": "external",
        }
        if parent_id is not None:
            txn_payload["parent_id"] = parent_id
        txn = await create_shopify_order_transaction(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            api_version=api_version,
            transaction=txn_payload,
        )
        return {"ok": True, "created": True, "transaction_id": txn.get("id")}
    except Exception as e:
        logger.warning(
            "shopify_refund_txn.create_failed order_id=%s shopify_order_id=%s gateway=%s err=%s",
            pivota_order_id,
            shopify_order_id,
            gateway,
            str(e),
        )
        annotation = await annotate_shopify_order_best_effort(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            api_version=api_version,
            note_attributes={
                "pivota_order_id": str(pivota_order_id or ""),
                "pivota_psp": gateway,
                "pivota_psp_refund_ref": refund_ref,
            },
            tags=["pivota-external-psp-refund"],
        )
        return {"ok": False, "created": False, "error": str(e), "annotation": annotation}

