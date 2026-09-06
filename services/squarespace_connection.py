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
from typing import Any, Callable, Dict, List, Optional

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
    """The Squarespace API could not be reached, or refused the credential.

    `status_code` carries the upstream HTTP status when there was one. A
    connect failure that reports only "connection failed" is indistinguishable
    between "the key is wrong" (401), "this deployment cannot reach Squarespace"
    (timeout), and "the endpoint is not what we assumed" (404) — and the
    reachability of `GET /1.0/authorization/website` with a per-site API key is
    itself an ASSUMED claim (docs/SQUARESPACE_TELEMETRY.md, row 4). Naming the
    status is what makes a wrong assumption diagnosable from the response.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SquarespaceUnauthorizedError(SquarespaceConnectionError):
    """Squarespace refused THIS credential (401/403).

    Distinguished from every other connection failure because it is the one a
    caller can do something about: a Developer-Platform OAuth access token is
    short-lived, and a store that also holds a per-site API key can fall back to
    it for READ calls rather than going dark until someone reconnects.
    """


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


def squarespace_read_tokens(credentials: Dict[str, Any]) -> List[str]:
    """Every credential this store can READ with, best first.

    The OAuth token leads: it is the identity that also carries webhook
    subscriptions, so a store holding both should exercise one identity rather
    than two. But a Developer-Platform access token is SHORT-LIVED (assumed
    ~30 minutes; docs/SQUARESPACE_TELEMETRY.md) and this repo has no refresh
    path yet, so preferring it unconditionally would take a store that holds a
    perfectly good per-site API key dark within the hour. Callers try these in
    order and fall back on 401/403.
    """
    tokens: List[str] = []
    for key in ("oauth_access_token", "api_key"):
        value = str(credentials.get(key) or "").strip()
        if value and value not in tokens:
            tokens.append(value)
    return tokens


def squarespace_request_token(credentials: Dict[str, Any]) -> str:
    """The FIRST token to read the Orders API with, or "" when there is none."""
    tokens = squarespace_read_tokens(credentials)
    return tokens[0] if tokens else ""


async def fetch_squarespace_website(
    access_token: str,
    *,
    timeout: float = SQUARESPACE_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
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

    async def _call(http: httpx.AsyncClient) -> Any:
        return await http.get(
            f"{SQUARESPACE_API_ROOT}{SQUARESPACE_AUTHORIZATION_PATH}",
            headers=build_squarespace_headers(token),
        )

    try:
        if client is not None:
            response = await _call(client)
        else:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as http:
                response = await _call(http)
    except httpx.HTTPError as exc:
        raise SquarespaceConnectionError(
            f"Squarespace authorization lookup failed: {exc}"
        ) from exc
    if response.status_code == 401 or response.status_code == 403:
        raise SquarespaceUnauthorizedError(
            "Squarespace refused the credential (check the key and its Orders scope)",
            status_code=response.status_code,
        )
    if response.status_code == 429:
        raise SquarespaceConnectionError(
            "Squarespace rate-limited the authorization lookup", status_code=429
        )
    if response.status_code != 200:
        raise SquarespaceConnectionError(
            f"Squarespace authorization lookup failed with HTTP {response.status_code}",
            status_code=response.status_code,
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
    updates: Optional[Dict[str, Any]] = None,
    mutate: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    mark_connected: bool = False,
    db: Any = None,
) -> Dict[str, Any]:
    """Read-modify-write the store's credential blob ATOMICALLY, then re-read it.

    Never an overwrite. The blob holds the webhook secret (the only copy Pivota
    has), the OAuth token, the website binding and the reconciliation cursor
    all in one cell; a whole-cell write is exactly the PrestaShop P1 where
    reconnecting a shop silently disarmed its telemetry.

    A read-modify-write is not enough on its own, which is what this function
    exists to fix. Read, mutate, write, re-read with no transaction is a
    LOST-UPDATE window: a sweep persisting its cursor between `ensure`'s read
    and its write erases the once-shown `webhook_secret` (after which every
    delivery 401s and no reconnect can recover it, because Squarespace shows
    that secret exactly once), and the reverse interleaving reverts a reconnect
    to the credential the merchant just replaced. So the whole cycle runs
    inside `database.transaction()` and, on Postgres, behind `SELECT ... FOR
    UPDATE` on the store row — the row lock is what makes a concurrent merge
    WAIT rather than read a value that is about to be stale.

    On SQLite there is no `FOR UPDATE`; the select is plain. That is tolerable
    only because SQLite here is tests and local development, and it is why the
    serialization claim is pinned in the Postgres gate
    (`tests/test_squarespace_ledger_postgres.py`) rather than in the SQLite
    suite, which cannot observe it.

    `updates` is merged over the stored blob. `mutate` receives the LOCKED blob
    and returns the one to persist, which is how the connect path expresses
    "drop these keys when the credential now belongs to a different site"
    inside the same critical section instead of hand-rolling a second one.
    `mark_connected` additionally refreshes the row's connect bookkeeping, so a
    reconnect is one statement rather than a merge racing an UPDATE.

    The re-read is not belt-and-braces: `databases` + asyncpg reports no
    rowcount from an UPDATE, so reading the row back is the only proof the
    write landed.
    """
    from db.database import IS_POSTGRES
    from db.database import database as default_database

    handle = db if db is not None else default_database
    select_sql = "SELECT api_key FROM merchant_stores WHERE store_id = :store_id"
    # The row lock is the whole point of the transaction: without it two merges
    # both read the pre-write blob and the second silently discards the first.
    locking_sql = f"{select_sql} FOR UPDATE" if IS_POSTGRES else select_sql
    assignments = "api_key = :api_key"
    if mark_connected:
        assignments += (
            ", status = 'active', last_sync = CURRENT_TIMESTAMP,"
            " connected_at = CURRENT_TIMESTAMP"
        )

    async with handle.transaction():
        row = await handle.fetch_one(locking_sql, {"store_id": store_id})
        credentials = parse_squarespace_credentials(
            dict(row).get("api_key") if row else None
        )
        if mutate is not None:
            credentials = dict(mutate(credentials))
        if updates:
            credentials.update(updates)
        await handle.execute(
            f"UPDATE merchant_stores SET {assignments} WHERE store_id = :store_id",
            {
                "store_id": store_id,
                "api_key": serialize_squarespace_credentials(credentials),
            },
        )
        persisted = await handle.fetch_one(select_sql, {"store_id": store_id})
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
