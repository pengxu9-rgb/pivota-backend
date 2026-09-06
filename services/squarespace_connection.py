"""Squarespace credential shape, HTTP envelope, and store-row persistence.

Squarespace has TWO authentication models and they do not reach the same APIs,
which is the single fact that shapes this whole integration:

* a per-site **API key** (Settings -> Developer API Keys, scoped per product)
  reads the Orders API. It cannot create webhook subscriptions.
* an **OAuth access token**, issued to an app on the Squarespace Developer
  Platform, reaches the Webhook Subscriptions API as well.

So an API-key store gets telemetry through the reconciliation sweep
(services/squarespace_order_sweep.py) and an OAuth store additionally gets
push. `POST /integrations/squarespace/{store_id}/webhooks/ensure` refuses with
409 `oauth_required` rather than pretending; see docs/SQUARESPACE_TELEMETRY.md
for the verified-vs-assumed table behind that claim.

The credential blob lives in `merchant_stores.api_key` as JSON, exactly like
BigCommerce and PrestaShop, so no schema change is needed::

    {"api_key": "...", "website_id": "...", "oauth_access_token": "...",
     "webhook_secret": "...", "webhook_subscription_id": "...",
     "reconciliation": {"orders_cursor": "...", "last_run_at": "..."}}
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx


logger = logging.getLogger("squarespace_connection")

SQUARESPACE_API_ROOT = "https://api.squarespace.com/1.0"
SQUARESPACE_AUTHORIZATION_PATH = "/authorization/website"
SQUARESPACE_TIMEOUT_SECONDS = 15.0

# Squarespace REQUIRES a User-Agent on every API call and answers a request
# without one with 400; it is not the usual optional courtesy header.
SQUARESPACE_USER_AGENT = "Pivota-Commerce-Telemetry/1.0"

PLATFORM = "squarespace"


class SquarespaceConnectionError(RuntimeError):
    """The Squarespace API could not be reached, or refused the credential."""


def build_squarespace_headers(access_token: str) -> Dict[str, str]:
    """Bearer + the REQUIRED User-Agent. Identical for API keys and OAuth tokens."""
    return {
        "Authorization": f"Bearer {str(access_token or '').strip()}",
        "User-Agent": SQUARESPACE_USER_AGENT,
        "Accept": "application/json",
    }


def parse_squarespace_credentials(raw: Any) -> Dict[str, Any]:
    """The credential JSON out of `merchant_stores.api_key`.

    A bare string is read as the API key so a row written by some other path
    is not silently treated as credential-less.
    """
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value:
        return {}
    if not value.startswith("{"):
        return {"api_key": value}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"api_key": value}
    return dict(parsed) if isinstance(parsed, dict) else {"api_key": value}


def serialize_squarespace_credentials(credentials: Dict[str, Any]) -> str:
    return json.dumps(credentials, separators=(",", ":"))


def squarespace_request_token(credentials: Dict[str, Any]) -> str:
    """The token to read the Orders API with.

    The OAuth token is preferred when both are present: it is the credential
    that also carries webhook subscriptions, so a store that has both should
    exercise one identity, not two.
    """
    return (
        str(credentials.get("oauth_access_token") or "").strip()
        or str(credentials.get("api_key") or "").strip()
    )


async def fetch_squarespace_website(
    access_token: str,
    *,
    timeout: float = SQUARESPACE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """`GET /1.0/authorization/website` — the site this credential belongs to.

    This is the connect-time validation AND the binding: the returned `id` is
    persisted as `website_id` and every webhook delivery must name it. Without
    that binding a notification signed by some other Squarespace site's secret
    could not be distinguished from this store's own.
    """
    token = str(access_token or "").strip()
    if not token:
        raise SquarespaceConnectionError("Squarespace credential is empty")
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"{SQUARESPACE_API_ROOT}{SQUARESPACE_AUTHORIZATION_PATH}",
                headers=build_squarespace_headers(token),
            )
    except httpx.HTTPError as exc:
        raise SquarespaceConnectionError(
            f"Squarespace authorization lookup failed: {exc}"
        ) from exc
    if response.status_code == 401 or response.status_code == 403:
        raise SquarespaceConnectionError(
            "Squarespace refused the credential (check the key and its Orders scope)"
        )
    if response.status_code == 429:
        raise SquarespaceConnectionError("Squarespace rate-limited the authorization lookup")
    if response.status_code != 200:
        raise SquarespaceConnectionError(
            f"Squarespace authorization lookup failed with HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise SquarespaceConnectionError("Invalid Squarespace authorization response") from exc
    if not isinstance(payload, dict):
        raise SquarespaceConnectionError("Invalid Squarespace authorization response")
    # The documented shape is the website object itself; a `{"website": {...}}`
    # envelope is accepted too so a future wrapper cannot turn a valid key into
    # a store with no website binding at all.
    website = payload.get("website") if isinstance(payload.get("website"), dict) else payload
    website_id = str(website.get("id") or "").strip()
    if not website_id:
        raise SquarespaceConnectionError(
            "Squarespace authorization response carries no website id"
        )
    return dict(website)


async def merge_squarespace_credentials(
    *,
    store_id: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """Read-modify-write the store's credential blob, then re-read it.

    Never an overwrite. The blob holds the webhook secret (the only copy Pivota
    has) and the reconciliation cursor alongside the API key; a whole-cell
    write is exactly the PrestaShop P1 where reconnecting a shop silently
    disarmed its telemetry.

    The re-read is not belt-and-braces: `databases` + asyncpg reports no
    rowcount from an UPDATE, so reading the row back is the only proof the
    write landed, and under a race it is the only way to learn which writer
    won.
    """
    from db.database import database

    row = await database.fetch_one(
        "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
        {"store_id": store_id},
    )
    credentials = parse_squarespace_credentials(dict(row).get("api_key") if row else None)
    credentials.update(updates)
    await database.execute(
        "UPDATE merchant_stores SET api_key = :api_key WHERE store_id = :store_id",
        {
            "store_id": store_id,
            "api_key": serialize_squarespace_credentials(credentials),
        },
    )
    persisted = await database.fetch_one(
        "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
        {"store_id": store_id},
    )
    return parse_squarespace_credentials(
        dict(persisted).get("api_key") if persisted else None
    )


async def find_squarespace_store(store_id: str) -> Optional[Dict[str, Any]]:
    """An ACTIVE Squarespace store row by id, or None."""
    from db.database import database

    row = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, name, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'squarespace'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": str(store_id or "").strip()},
    )
    return dict(row) if row else None


async def active_squarespace_stores() -> list[Dict[str, Any]]:
    """Every active Squarespace store. Used by the sweep's all-stores mode."""
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT store_id, merchant_id, domain, name, api_key
        FROM merchant_stores
        WHERE platform = 'squarespace'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """
    )
    return [dict(row) for row in rows]
