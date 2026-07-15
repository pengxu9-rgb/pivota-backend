import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db.database import database
from services.merchant_store_service import get_primary_store
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.shopify_graphql_client import ShopifyGraphQLError, shopify_admin_graphql
from services.shopify_policy_service import (
    fetch_and_store_shop_policies,
    get_latest_policy_hashes,
    summarize_policies_rest_response,
)

logger = logging.getLogger(__name__)


DEFAULT_API_VERSION = "2025-10"

# v0.1 minimal required scopes for PCS ingestion + webhook registration.
REQUIRED_SCOPES = [
    "read_orders",
]

# Optional scopes that raise the ceiling (disputes/payouts/returns/policies).
OPTIONAL_SCOPES = [
    "read_products",
    "read_discounts",
    "read_customers",
    "read_fulfillments",
    "read_returns",
    "read_shopify_payments_disputes",
    "read_shopify_payments_payouts",
    "read_shopify_payments_balance",
    # Shopify policies are typically gated by read_legal_policies (some docs mention read_content).
    "read_legal_policies",
    "read_content",
]


SHOP_QUERY_REST = "https://{shop_domain}/admin/api/{api_version}/shop.json"
ACCESS_SCOPES_REST = "https://{shop_domain}/admin/oauth/access_scopes.json"
WEBHOOKS_CREATE_REST = "https://{shop_domain}/admin/api/{api_version}/webhooks.json"
POLICIES_REST = "https://{shop_domain}/admin/api/{api_version}/policies.json"


async def _shopify_get_json(*, url: str, access_token: str, timeout_s: float = 12.0) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        if resp.status_code >= 400:
            raise RuntimeError(f"Shopify HTTP {resp.status_code}")
        return resp.json()


async def _shopify_get_json_with_diag(
    *, url: str, access_token: str, timeout_s: float = 12.0
) -> Dict[str, Any]:
    """
    Return a safe diagnostics wrapper: status + minimal JSON shape info (no raw body leakage).
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        out: Dict[str, Any] = {"status_code": resp.status_code}
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            out["json_keys"] = sorted(list(data.keys()))[:25]
        else:
            out["json_type"] = type(data).__name__
        if resp.status_code >= 400:
            out["error"] = (resp.text or "")[:200]
        else:
            out["summary"] = summarize_policies_rest_response(data)
        return out


async def fetch_access_scopes(
    *, shop_domain: str, access_token: str, timeout_s: float = 12.0
) -> List[str]:
    url = ACCESS_SCOPES_REST.format(shop_domain=shop_domain, api_version=DEFAULT_API_VERSION)
    data = await _shopify_get_json(url=url, access_token=access_token, timeout_s=timeout_s)
    scopes = []
    for item in (data or {}).get("access_scopes") or []:
        handle = item.get("handle")
        if handle:
            scopes.append(str(handle))
    return sorted(set(scopes))


async def register_webhooks_best_effort(
    *,
    shop_domain: str,
    access_token: str,
    merchant_id: str,
    callback_base_url: str,
    topics: List[str],
    api_version: str = DEFAULT_API_VERSION,
    timeout_s: float = 15.0,
) -> Dict[str, Any]:
    """
    Best-effort REST webhook registration.
    Shopify REST create is not strictly idempotent; we treat 201 as created and 422(address taken) as already exists.
    """
    url = WEBHOOKS_CREATE_REST.format(shop_domain=shop_domain, api_version=api_version)
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}

    created: List[Dict[str, Any]] = []
    already_exists: List[str] = []
    failed: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for topic in topics:
            payload = {
                "webhook": {
                    "topic": topic,
                    "address": f"{callback_base_url.rstrip('/')}/webhooks/shopify/{merchant_id}",
                    "format": "json",
                }
            }
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 201:
                body = resp.json() or {}
                webhook = body.get("webhook") or {}
                created.append({"topic": topic, "webhook_id": webhook.get("id")})
                continue

            if resp.status_code == 422:
                # Common response: {"errors":{"address":["has already been taken"]}}
                try:
                    body = resp.json() or {}
                    errors = body.get("errors") or {}
                    address_errors = errors.get("address") or []
                    if isinstance(address_errors, list) and any("already" in str(x).lower() for x in address_errors):
                        already_exists.append(topic)
                        continue
                except Exception:
                    pass

            failed.append(
                {
                    "topic": topic,
                    "status_code": resp.status_code,
                    "body": (resp.text or "")[:800],
                }
            )

    return {"created": created, "already_exists": already_exists, "failed": failed}


async def probe_shopify_payments_capability(
    *, shop_domain: str, access_token: str, api_version: str = DEFAULT_API_VERSION
) -> bool:
    """
    Best-effort probe: if shopifyPaymentsDisputes query succeeds, we assume Shopify Payments is enabled + scope present.
    """
    probe_query = """
    query ProbeDisputes {
      shopifyPaymentsDisputes(first: 1) { nodes { id } }
    }
    """
    try:
        await shopify_admin_graphql(
            shop_domain=shop_domain,
            access_token=access_token,
            query=probe_query,
            api_version=api_version,
        )
        return True
    except Exception:
        return False


async def probe_returns_capability(
    *, shop_domain: str, access_token: str, api_version: str = DEFAULT_API_VERSION
) -> bool:
    try:
        # Shopify Returns availability is schema-gated; some shops expose returns under `Shop.returns`
        # or `Order.returns` even when `QueryRoot.returns` is not present.
        introspect_query = """
        query TypeFields($name: String!) {
          __type(name: $name) { fields { name } }
        }
        """
        q = await shopify_admin_graphql(
            shop_domain=shop_domain,
            access_token=access_token,
            query=introspect_query,
            variables={"name": "QueryRoot"},
            api_version=api_version,
        )
        q_fields = (((q or {}).get("__type") or {}).get("fields")) or []
        q_names = {str(f.get("name")) for f in q_fields if isinstance(f, dict) and f.get("name")}
        if "returns" in q_names:
            return True

        s = await shopify_admin_graphql(
            shop_domain=shop_domain,
            access_token=access_token,
            query=introspect_query,
            variables={"name": "Shop"},
            api_version=api_version,
        )
        s_fields = (((s or {}).get("__type") or {}).get("fields")) or []
        s_names = {str(f.get("name")) for f in s_fields if isinstance(f, dict) and f.get("name")}
        if "returns" in s_names:
            return True

        o = await shopify_admin_graphql(
            shop_domain=shop_domain,
            access_token=access_token,
            query=introspect_query,
            variables={"name": "Order"},
            api_version=api_version,
        )
        o_fields = (((o or {}).get("__type") or {}).get("fields")) or []
        o_names = {str(f.get("name")) for f in o_fields if isinstance(f, dict) and f.get("name")}
        return "returns" in o_names
    except Exception:
        return False


def _missing_scopes(scopes: List[str], required: List[str]) -> List[str]:
    present = set(scopes)
    return [s for s in required if s not in present]


def _policy_scope_present(scopes: List[str]) -> bool:
    present = set(scopes)
    return ("read_legal_policies" in present) or ("read_content" in present)


def _isoformat_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return None
    return None


def _policies_report_json_safe(policies_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make policies_report safe for JSONB storage (no datetime objects).
    """
    if not isinstance(policies_report, dict):
        return {"skipped": False, "error": "invalid policies_report type"}

    out = dict(policies_report)

    latest_hashes_safe: List[Dict[str, Any]] = []
    for row in (policies_report.get("latest_hashes") or []):
        if not isinstance(row, dict):
            continue
        latest_hashes_safe.append(
            {
                "policy_type": row.get("policy_type"),
                "url": row.get("url"),
                "updated_at": _isoformat_or_none(row.get("updated_at")),
                "hash_sha256": row.get("hash_sha256"),
                "fetched_at": _isoformat_or_none(row.get("fetched_at")),
            }
        )
    if "latest_hashes" in out:
        out["latest_hashes"] = latest_hashes_safe
    return out


async def upsert_merchant_capabilities(
    *,
    merchant_id: str,
    shopify_api_version: str,
    scopes_json: Dict[str, Any],
    has_shopify_payments: bool,
    has_returns_api: bool,
) -> None:
    scopes_payload: Any = scopes_json
    # Accept legacy callers that pass a JSON string.
    if isinstance(scopes_payload, str):
        try:
            scopes_payload = json.loads(scopes_payload)
        except Exception:
            scopes_payload = {"raw": scopes_payload}
    # NOTE: The `databases` + `asyncpg` stack expects JSON/JSONB bind params as `str`,
    # not Python `dict`. Serialize explicitly to avoid silent upsert failures.
    if not isinstance(scopes_payload, str):
        scopes_payload = json.dumps(scopes_payload, ensure_ascii=False)
    await database.execute(
        """
        INSERT INTO pcs_merchant_capabilities
          (merchant_id, shopify_api_version, scopes_json, has_shopify_payments, has_returns_api, last_checked_at)
        VALUES
          (:merchant_id, :shopify_api_version, CAST(:scopes_json AS jsonb), :has_shopify_payments, :has_returns_api, NOW())
        ON CONFLICT (merchant_id) DO UPDATE SET
          shopify_api_version = EXCLUDED.shopify_api_version,
          scopes_json = EXCLUDED.scopes_json,
          has_shopify_payments = EXCLUDED.has_shopify_payments,
          has_returns_api = EXCLUDED.has_returns_api,
          last_checked_at = EXCLUDED.last_checked_at
        """,
        {
            "merchant_id": merchant_id,
            "shopify_api_version": shopify_api_version,
            "scopes_json": scopes_payload,
            "has_shopify_payments": has_shopify_payments,
            "has_returns_api": has_returns_api,
        },
    )


async def verify_shopify_integration(
    *,
    merchant_id: str,
    callback_base_url: str,
    api_version: str = DEFAULT_API_VERSION,
) -> Dict[str, Any]:
    run_id = str(uuid.uuid4())
    store_info = await get_primary_store(merchant_id)
    if not store_info or (store_info.get("platform") or "").lower() != "shopify":
        raise ValueError("Primary store is not Shopify")

    shop_domain = store_info.get("domain") or ""
    access_token, _ = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
        store_id=str(store_info.get("store_id") or "").strip() or None,
    )
    access_token = (access_token or "").strip()
    store_id = store_info.get("store_id")

    if not shop_domain or not access_token:
        raise ValueError("Missing Shopify credentials")

    checked_at = datetime.now(timezone.utc).isoformat()

    # 1) Token validity + canonical myshopify domain.
    shop_url = SHOP_QUERY_REST.format(shop_domain=shop_domain, api_version=api_version)
    shop_json = await _shopify_get_json(url=shop_url, access_token=access_token)
    shop = (shop_json or {}).get("shop") or {}

    canonical_myshopify = shop.get("myshopify_domain") or ""
    domain_updated: Optional[Tuple[str, str]] = None
    if canonical_myshopify and canonical_myshopify.strip().lower() != shop_domain.strip().lower():
        # Update stored domain to myshopify_domain to match webhook header X-Shopify-Shop-Domain.
        try:
            if store_id:
                await database.execute(
                    "UPDATE merchant_stores SET domain = :domain WHERE store_id = :store_id",
                    {"domain": canonical_myshopify, "store_id": store_id},
                )
                domain_updated = (shop_domain, canonical_myshopify)
                shop_domain = canonical_myshopify
        except Exception as e:
            logger.warning("Failed to canonicalize shop domain merchant=%s store_id=%s: %s", merchant_id, store_id, e)

    # 2) Scopes check.
    scopes: List[str] = []
    scopes_error: Optional[str] = None
    try:
        scopes = await fetch_access_scopes(shop_domain=shop_domain, access_token=access_token)
    except Exception as e:
        scopes_error = str(e)

    missing_required = _missing_scopes(scopes, REQUIRED_SCOPES) if scopes else REQUIRED_SCOPES
    missing_optional = _missing_scopes(scopes, OPTIONAL_SCOPES) if scopes else OPTIONAL_SCOPES

    # 3) Webhook registration best-effort (requires write_webhooks).
    topics = [
        "orders/create",
        "orders/updated",
        "orders/paid",
        "orders/cancelled",
        "fulfillments/create",
        "fulfillments/update",
        "orders/fulfilled",
        "refunds/create",
        "tender_transactions/create",
        "disputes/create",
        "disputes/update",
        "returns/create",
        "returns/update",
        # NOTE: compliance topics (customers/data_request, customers/redact, shop/redact)
        # are toml/dashboard-managed app-config subscriptions and CANNOT be REST-registered
        # (Shopify rejects them via the webhooks API → permanent `failed` noise). Omitted here.
    ]
    # Shopify access scopes UI is inconsistent across app types; instead of hard-gating on scopes,
    # we attempt registration and report failures (401/403/422) to make onboarding deterministic.
    webhook_report: Dict[str, Any] = {"attempted": True}
    try:
        webhook_report.update(
            await register_webhooks_best_effort(
                shop_domain=shop_domain,
                access_token=access_token,
                merchant_id=merchant_id,
                callback_base_url=callback_base_url,
                topics=topics,
                api_version=api_version,
            )
        )
    except Exception as e:
        webhook_report.update({"error": str(e)})

    # 4) Policies snapshot (best-effort; requires policy scope).
    policies_report: Dict[str, Any] = {"skipped": True, "reason": "missing policy read scope"}
    if _policy_scope_present(scopes):
        try:
            fetched = await fetch_and_store_shop_policies(
                merchant_id=merchant_id,
                shop_domain=shop_domain,
                access_token=access_token,
                api_version=api_version,
            )
            # Extra safe diagnostics: help distinguish "no policies configured" vs parsing/permission issues.
            policies_diag = await _shopify_get_json_with_diag(
                url=POLICIES_REST.format(shop_domain=shop_domain, api_version=api_version),
                access_token=access_token,
            )
            policies_report = {
                "skipped": False,
                "latest_hashes": await get_latest_policy_hashes(merchant_id),
                "fetched_types": sorted(list((fetched or {}).keys())),
                "policies_rest_diag": policies_diag,
            }
        except ShopifyGraphQLError as e:
            # Surface GraphQL errors for actionable onboarding debugging.
            policies_report = {
                "skipped": False,
                "error": e.message,
                "graphql_errors": e.errors[:5],
                "request_id": e.request_id,
            }
        except Exception as e:
            policies_report = {"skipped": False, "error": str(e)}

    # 5) Capabilities probes (best-effort).
    has_shopify_payments = False
    has_returns_api = False
    try:
        has_shopify_payments = await probe_shopify_payments_capability(
            shop_domain=shop_domain, access_token=access_token, api_version=api_version
        )
    except Exception:
        has_shopify_payments = False

    try:
        has_returns_api = await probe_returns_capability(
            shop_domain=shop_domain, access_token=access_token, api_version=api_version
        )
    except Exception:
        has_returns_api = False

    scopes_json = {
        "run_id": run_id,
        "checked_at": checked_at,
        "shop_domain": shop_domain,
        "access_scopes": scopes,
        "missing_required_scopes": missing_required,
        "missing_optional_scopes": missing_optional,
        "scopes_error": scopes_error,
        "domain_canonicalized": {"from": domain_updated[0], "to": domain_updated[1]} if domain_updated else None,
        "webhooks": webhook_report,
        # Ensure JSON-safe types for DB storage (no datetimes).
        "policies": _policies_report_json_safe(policies_report),
    }

    # Persist latest snapshot for onboarding gating / tier ceiling.
    try:
        await upsert_merchant_capabilities(
            merchant_id=merchant_id,
            shopify_api_version=api_version,
            scopes_json=scopes_json,
            has_shopify_payments=has_shopify_payments,
            has_returns_api=has_returns_api,
        )
    except Exception as e:
        logger.warning("Failed to upsert pcs_merchant_capabilities merchant=%s: %s", merchant_id, e)

    return {
        "run_id": run_id,
        "merchant_id": merchant_id,
        "checked_at": checked_at,
        "shop": {
            "name": shop.get("name"),
            "myshopify_domain": shop.get("myshopify_domain"),
            "domain": shop.get("domain"),
            "currency": shop.get("currency"),
            "primary_locale": shop.get("primary_locale"),
        },
        "scopes": {
            "access_scopes": scopes,
            "missing_required": missing_required,
            "missing_optional": missing_optional,
            "scopes_error": scopes_error,
        },
        "webhooks": webhook_report,
        "policies": policies_report,
        "capabilities": {
            "has_shopify_payments": has_shopify_payments,
            "has_returns_api": has_returns_api,
        },
    }
