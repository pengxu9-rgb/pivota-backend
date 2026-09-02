from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2025-10"


class ShopifyTransactionSyncError(Exception):
    pass


class ShopifyTransactionSyncAuthError(ShopifyTransactionSyncError):
    pass


class ShopifyTransactionSyncRateLimitError(ShopifyTransactionSyncError):
    pass


class ShopifyTransactionSyncHttpError(ShopifyTransactionSyncError):
    def __init__(self, *, status_code: int, response_text: str):
        self.status_code = int(status_code)
        self.response_text = str(response_text or "")
        super().__init__(
            f"Failed to create transaction (status={self.status_code}) {self.response_text}"
        )


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


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except Exception:
        return None


def _find_successful_parent_transaction(
    txns: List[Dict[str, Any]],
    *,
    preferred_gateway: Optional[str] = None,
) -> Dict[str, Any]:
    preferred_gateway = str(preferred_gateway or "").strip().lower() or None
    fallback: Optional[Dict[str, Any]] = None
    for t in reversed(txns):
        if not isinstance(t, dict):
            continue
        kind = str(t.get("kind") or "").lower()
        status = str(t.get("status") or "").lower()
        if kind not in ("sale", "capture") or status != "success":
            continue
        tid = _coerce_int(t.get("id"))
        if tid is None:
            continue
        gateway = str(t.get("gateway") or "").strip().lower() or None
        candidate = {"id": tid, "gateway": gateway}
        if preferred_gateway and gateway == preferred_gateway:
            return candidate
        if fallback is None:
            fallback = candidate
    return fallback or {}


async def ensure_shopify_parent_transaction_best_effort(
    *,
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    amount: float,
    currency: str,
    api_version: str = DEFAULT_API_VERSION,
    txns: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    observed = list(txns or [])
    existing = _find_successful_parent_transaction(observed)
    if existing.get("id") is not None:
        return {
            "ok": True,
            "created": False,
            "transaction_id": existing["id"],
            "gateway": existing.get("gateway"),
            "source": "existing_success_transaction",
        }

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
                "gateway": "manual",
            },
        )
    except Exception as e:
        return {"ok": False, "created": False, "error": str(e), "source": "manual_parent_create_failed"}

    txn_id = _coerce_int(txn.get("id"))
    if txn_id is None:
        return {"ok": False, "created": False, "error": "missing_transaction_id", "source": "manual_parent_create_failed"}

    return {
        "ok": True,
        "created": True,
        "transaction_id": txn_id,
        "gateway": str(txn.get("gateway") or "manual").strip().lower() or "manual",
        "source": "created_manual_parent",
    }


def _is_non_fatal_invalid_sale_error(err: Exception) -> bool:
    if not isinstance(err, ShopifyTransactionSyncHttpError):
        return False
    if err.status_code != 422:
        return False
    return "sale is not a valid transaction" in err.response_text.lower()


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
        raise ShopifyTransactionSyncHttpError(
            status_code=resp.status_code,
            response_text=_safe_text(resp),
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
            return {
                "ok": True,
                "created": False,
                "transaction_id": t.get("id"),
                "parent_transaction_id": _coerce_int(t.get("id")),
                "parent_transaction_gateway": str(t.get("gateway") or "").strip().lower() or gateway,
                "parent_transaction_source": "existing_authorization",
            }

    # Some Shopify orders are already recorded as external/manual sales when created.
    # If the authorization already exists on another gateway, treat it as already synced.
    for t in txns:
        if not isinstance(t, dict):
            continue
        if str(t.get("authorization") or "").strip() != payment_ref:
            continue
        status = str(t.get("status") or "").lower()
        if status and status != "success":
            continue
        return {
            "ok": True,
            "created": False,
            "transaction_id": t.get("id"),
            "matched_existing_authorization": True,
            "existing_gateway": t.get("gateway"),
            "parent_transaction_id": _coerce_int(t.get("id")),
            "parent_transaction_gateway": str(t.get("gateway") or "").strip().lower() or None,
            "parent_transaction_source": "existing_authorization_other_gateway",
        }

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
        return {
            "ok": True,
            "created": True,
            "transaction_id": txn.get("id"),
            "parent_transaction_id": _coerce_int(txn.get("id")),
            "parent_transaction_gateway": str(txn.get("gateway") or "").strip().lower() or gateway,
            "parent_transaction_source": "created_external_sale",
        }
    except Exception as e:
        if _is_non_fatal_invalid_sale_error(e):
            parent_sync = await ensure_shopify_parent_transaction_best_effort(
                shop_domain=shop_domain,
                access_token=access_token,
                shopify_order_id=shopify_order_id,
                amount=amount,
                currency=currency,
                api_version=api_version,
                txns=txns,
            )
            logger.info(
                "shopify_txn.create_soft_skipped order_id=%s shopify_order_id=%s gateway=%s reason=invalid_sale_kind",
                pivota_order_id,
                shopify_order_id,
                gateway,
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
            return {
                "ok": True,
                "created": False,
                "soft_skipped": True,
                "reason": "invalid_sale_kind",
                "annotation": annotation,
                "parent_transaction_id": parent_sync.get("transaction_id"),
                "parent_transaction_gateway": parent_sync.get("gateway"),
                "parent_transaction_source": parent_sync.get("source"),
                "parent_transaction_created": parent_sync.get("created"),
                "parent_transaction_error": parent_sync.get("error"),
            }
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
    parent_transaction_id: Optional[int] = None,
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
        # The existing-transaction list is the ONLY thing preventing a duplicate
        # refund row. Failing to read it is "cannot verify", not "nothing is
        # there" — and with an explicit `parent_transaction_id` the code below
        # would otherwise sail past the dedupe loop on an empty list and create
        # a second refund. Refuse instead; the caller retries.
        return {
            "ok": False,
            "skipped": True,
            "retryable": True,
            "reason": "transaction_list_unavailable",
        }

    # Dedupe on the refund reference ALONE — deliberately NOT on gateway.
    #
    # This loop used to skip any transaction whose gateway differed from
    # `normalize_shopify_gateway(psp_used)`, while the create below writes
    # `parent_gateway or gateway`. Whenever the parent sale's gateway differs
    # from psp_used — a `manual` parent (which this module itself creates when
    # Shopify 422s "sale is not a valid transaction"), `shopify_payments`, COD —
    # the row this function created could never match its own dedupe filter, so
    # every call created another refund transaction for the same refund.
    # Measured: 3 calls, 3 identical refund rows.
    #
    # `authorization` holds the external PSP's refund id, which is unique per
    # refund, so it is the correct identity here. Two distinct refunds never
    # share one; the same refund must never be written twice whatever gateway
    # label its row carries.
    for t in txns:
        if not isinstance(t, dict):
            continue
        # Only a refund row can be the refund we are about to write. The gateway
        # conjunct removed above was incidentally providing this narrowing; keep
        # it explicitly so an unrelated transaction that happens to carry the
        # same authorization cannot read as "already refunded".
        if str(t.get("kind") or "").strip().lower() != "refund":
            continue
        if str(t.get("authorization") or "").strip() == refund_ref:
            return {
                "ok": True,
                "created": False,
                "transaction_id": t.get("id"),
                "transaction_gateway": t.get("gateway"),
                "parent_transaction_id": parent_transaction_id,
            }

    parent_id = _coerce_int(parent_transaction_id)
    parent_gateway: Optional[str] = None
    if parent_id is not None:
        for t in txns:
            if not isinstance(t, dict):
                continue
            if _coerce_int(t.get("id")) != parent_id:
                continue
            parent_gateway = str(t.get("gateway") or "").strip().lower() or None
            break

    if parent_id is None:
        preferred_parent = _find_successful_parent_transaction(txns, preferred_gateway=gateway)
        parent_id = preferred_parent.get("id")
        parent_gateway = preferred_parent.get("gateway")

    if parent_id is None:
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
            tags=["pivota-external-psp-refund", "pivota-missing-parent-transaction"],
        )
        return {
            "ok": True,
            "created": False,
            "soft_skipped": True,
            "reason": "missing_parent_transaction",
            "annotation": annotation,
        }

    try:
        refund_gateway = parent_gateway or gateway
        txn_payload: Dict[str, Any] = {
            "kind": "refund",
            "status": "success",
            "amount": str(amount),
            "currency": str(currency or "USD"),
            "gateway": refund_gateway,
            "authorization": refund_ref,
            "source_name": "external",
        }
        txn_payload["parent_id"] = parent_id
        txn = await create_shopify_order_transaction(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            api_version=api_version,
            transaction=txn_payload,
        )
        return {
            "ok": True,
            "created": True,
            "transaction_id": txn.get("id"),
            "parent_transaction_id": parent_id,
            "parent_transaction_gateway": refund_gateway,
        }
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
