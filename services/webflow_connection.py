"""Webflow credential shape, HTTP envelope, site binding, and store lookups.

Webflow's Data API v2 has one Bearer credential with two provenances that reach
the same endpoints but differ in ONE way that decides this integration's auth
model:

* a **Site API token** (Site settings -> Apps & integrations -> API access),
  scoped per site;
* an **OAuth App token** issued to a Data Client app.

Both read `/v2/sites/{site_id}/orders` and both can create webhooks. But Webflow
signs a webhook delivery only when the webhook was created by an OAuth App, and
it signs it with that **App's client secret** — which a site-token installation
does not have. So a signature check cannot be the only thing standing in front
of the receiver; see routes/webflow_webhooks.py and
docs/WEBFLOW_TELEMETRY.md for the two-layer answer.

No schema change. The credential blob lives in `merchant_stores.api_key` as
JSON, exactly like BigCommerce, PrestaShop and Squarespace::

    {"api_token": "...", "site_id": "...", "site_name": "...",
     "url_secret": "...", "webhook_ids": {"ecomm_new_order": "..."},
     "reconciliation": {"orders": {...}, "refunded": {...}}}

and it is written ONLY through `merge_webflow_credentials`, the shared atomic
read-modify-write in services/merchant_store_credentials.py.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any, Callable, Dict, List, Optional

import httpx

from services.merchant_store_credentials import (
    merge_store_credentials,
    parse_store_credentials,
    serialize_store_credentials,
)


logger = logging.getLogger("webflow_connection")

WEBFLOW_API_ROOT = "https://api.webflow.com/v2"
WEBFLOW_SITES_PATH = "/sites"
WEBFLOW_TIMEOUT_SECONDS = 15.0
# The site list, paged and bounded. 10 pages is 1,000 sites on one token, well
# past any real Webflow workspace.
_SITE_PAGE_LIMIT = 100
_MAX_SITE_PAGES = 10

PLATFORM = "webflow"

# Webflow ids are 24-character hex ObjectIds today. The pattern is deliberately
# wider than that (it is an ASSUMED shape, see docs/WEBFLOW_TELEMETRY.md) but
# still narrow enough that a value out of a webhook body can never walk a URL
# path: `..` is not matchable and `/` is not in the class.
WEBFLOW_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The per-store secret embedded in the webhook URL path. 32 bytes of
# `token_urlsafe` is 256 bits; it is the ONLY layer that authenticates a
# delivery for a site-token installation, so it is sized as a credential rather
# than as an id.
URL_SECRET_BYTES = 32

# Every blob key that either IS a credential the read path will use, or is
# state DERIVED from one particular Webflow site. On a reconnect that points a
# store at a different site, all of them are dropped.
#
# The dangerous half of this list is the credential itself. The Squarespace
# review found exactly this: a reconnect that dropped the derived state but left
# the OLD site's token behind kept every read reaching the old site, and its
# orders were then filed under the store that now represents the new one —
# well-formed rows belonging to somebody else's shop, with no downstream signal.
# `tests/test_webflow_connection.py` pins that every key
# `webflow_read_tokens` can read is a member here, so a second credential added
# later cannot quietly escape the drop.
WEBFLOW_SITE_SCOPED_KEYS = (
    # credentials the read path prefers
    "api_token",
    # the binding itself and everything derived from it
    "site_id",
    "site_name",
    "url_secret",
    "webhook_ids",
    "reconciliation",
)

# The keys, in preference order, that `webflow_read_tokens` will read with.
# Named separately from the tuple above so the parity test compares two
# independent statements rather than one restated.
WEBFLOW_TOKEN_KEYS = ("api_token",)


class WebflowConnectionError(RuntimeError):
    """Webflow could not be reached, or refused the credential.

    `status_code` carries the upstream HTTP status when there was one. A connect
    failure reporting only "connection failed" cannot be told apart from a
    mistyped token (401), a deployment that cannot reach Webflow (timeout), or
    an endpoint that is not what we assumed (404). Naming the status is what
    makes a wrong assumption diagnosable from the first response instead of from
    a support thread.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WebflowUnauthorizedError(WebflowConnectionError):
    """Webflow refused THIS credential (401/403)."""


class WebflowSiteAmbiguousError(WebflowConnectionError):
    """The token reaches zero or several sites and the caller named none.

    Carries the candidates so the connect route can answer with a list the
    merchant can choose from, rather than picking one for them: binding the
    wrong site would file another shop's orders under this store.
    """

    def __init__(self, message: str, *, sites: List[Dict[str, Any]]) -> None:
        super().__init__(message)
        self.sites = sites


def build_webflow_headers(api_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {str(api_token or '').strip()}",
        "Accept": "application/json",
    }


def parse_webflow_credentials(raw: Any) -> Dict[str, Any]:
    """The credential JSON out of `merchant_stores.api_key`.

    A bare string is read as the API TOKEN — Webflow's own credential field —
    rather than as the shared helper's default `api_key`: a string parked in a
    key the read path never looks at is indistinguishable from no credential at
    all.
    """
    return parse_store_credentials(raw, bare_key="api_token")


def serialize_webflow_credentials(credentials: Dict[str, Any]) -> str:
    return serialize_store_credentials(credentials)


def webflow_read_tokens(credentials: Dict[str, Any]) -> List[str]:
    """Every credential this store can READ with, best first.

    Webflow has one today. It is still a LIST, and the list is derived from
    `WEBFLOW_TOKEN_KEYS`, because the reconnect drop and the sweep's site check
    both have to enumerate "everything a read could use" and an
    open-coded single key is exactly what a second credential would slip past.
    """
    tokens: List[str] = []
    for key in WEBFLOW_TOKEN_KEYS:
        value = str(credentials.get(key) or "").strip()
        if value and value not in tokens:
            tokens.append(value)
    return tokens


def mint_url_secret() -> str:
    return secrets.token_urlsafe(URL_SECRET_BYTES)


def is_webflow_id(value: Any) -> bool:
    return bool(WEBFLOW_ID_PATTERN.match(str(value or "").strip()))


def _site_summary(site: Any) -> Dict[str, Any]:
    """The fields of a Webflow site object this repo keeps. Never the whole thing."""
    if not isinstance(site, dict):
        return {}
    return {
        "id": str(site.get("id") or "").strip(),
        "displayName": str(site.get("displayName") or "").strip() or None,
        "shortName": str(site.get("shortName") or "").strip() or None,
    }


async def _get(
    path: str,
    *,
    api_token: str,
    timeout: float,
    client: Optional[httpx.AsyncClient],
    what: str,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    token = str(api_token or "").strip()
    if not token:
        raise WebflowConnectionError("Webflow credential is empty")

    async def _call(http: httpx.AsyncClient) -> httpx.Response:
        # Plain `.get`, not `.request(method, ...)`: every read in this module is
        # a GET, and the narrower call is the one a caller-supplied client
        # double has to implement, which keeps the sweep's tests driving the
        # real request-building code instead of a stub of it.
        #
        # `params` is passed only when there are any, so the site LOOKUP (which
        # takes none) still issues the exact two-argument call every existing
        # double implements.
        if params:
            return await http.get(
                f"{WEBFLOW_API_ROOT}{path}",
                headers=build_webflow_headers(token),
                params=params,
            )
        return await http.get(
            f"{WEBFLOW_API_ROOT}{path}", headers=build_webflow_headers(token)
        )

    try:
        if client is not None:
            response = await _call(client)
        else:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, trust_env=False
            ) as http:
                response = await _call(http)
    except httpx.HTTPError as exc:
        raise WebflowConnectionError(f"Webflow {what} failed: {exc}") from exc
    if response.status_code in (401, 403):
        raise WebflowUnauthorizedError(
            "Webflow refused the credential (check the token and its "
            "ecommerce:read / sites:read scopes)",
            status_code=response.status_code,
        )
    if response.status_code == 429:
        # ~60 requests/min per token. The caller decides whether to back off;
        # naming it here keeps a rate limit from reading as a bad credential.
        raise WebflowConnectionError(
            f"Webflow rate-limited the {what}", status_code=429
        )
    if response.status_code != 200:
        raise WebflowConnectionError(
            f"Webflow {what} failed with HTTP {response.status_code}",
            status_code=response.status_code,
        )
    try:
        return response.json()
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise WebflowConnectionError(f"Invalid Webflow {what} response") from exc


async def list_webflow_sites(
    api_token: str,
    *,
    timeout: float = WEBFLOW_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """`GET /v2/sites` — every site this token can reach, walking past page 1.

    The list decides two things: whether the token resolves to exactly ONE site
    (`resolve_webflow_site`), and which candidates a 409 offers the merchant to
    choose from. Reading only the first page would make a token that reaches
    more sites than fit on one page silently resolve against a subset — and the
    dangerous half of that is the AMBIGUITY check, which is what stops a store
    being bound to a site the merchant did not mean.

    Bounded, and it stops on a SHORT page, so an endpoint that ignores
    `offset`/`limit` and answers everything at once ends the walk in one call.
    """
    sites: List[Dict[str, Any]] = []
    seen: set = set()
    offset = 0
    for _page in range(_MAX_SITE_PAGES):
        payload = await _get(
            WEBFLOW_SITES_PATH,
            api_token=api_token,
            timeout=timeout,
            client=client,
            what="site list",
            params={"offset": offset, "limit": _SITE_PAGE_LIMIT},
        )
        raw = payload.get("sites") if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise WebflowConnectionError("Invalid Webflow site list response")
        added = 0
        for site in (_site_summary(row) for row in raw):
            key = site.get("id")
            if not key or key in seen:
                continue
            seen.add(key)
            sites.append(site)
            added += 1
        if len(raw) < _SITE_PAGE_LIMIT or not added:
            break
        offset += len(raw)
    return sites


async def fetch_webflow_site(
    api_token: str,
    site_id: str,
    *,
    timeout: float = WEBFLOW_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """`GET /v2/sites/{site_id}` — one site, PROVING this token reaches it.

    This is both the connect-time validation of a caller-supplied `site_id` and
    the sweep's per-run binding check. The id is validated before it is
    interpolated: it comes from a request body at connect time and from the
    stored blob afterwards, and neither is a reason to build a URL out of it
    unchecked.
    """
    key = str(site_id or "").strip()
    if not is_webflow_id(key):
        raise WebflowConnectionError("Webflow site id is not a valid identifier")
    payload = await _get(
        f"{WEBFLOW_SITES_PATH}/{key}",
        api_token=api_token,
        timeout=timeout,
        client=client,
        what="site lookup",
    )
    site = _site_summary(payload if isinstance(payload, dict) else None)
    if not site.get("id"):
        raise WebflowConnectionError("Webflow site lookup response carries no site id")
    if site["id"] != key:
        # Never trust the echo over the request. If this ever fires, the URL was
        # not addressing the site we asked for.
        raise WebflowConnectionError(
            f"Webflow site lookup returned a different site ({site['id']})"
        )
    return site


async def resolve_webflow_site(
    api_token: str,
    *,
    site_id: Optional[str] = None,
    timeout: float = WEBFLOW_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """The site to bind this store to.

    With an explicit `site_id`, that site is looked up and the token is proven
    to reach it. Without one, the site is resolved ONLY when the token reaches
    exactly one site; zero or several raise `WebflowSiteAmbiguousError` with the
    candidates. Picking the first of several would silently bind a store to a
    site the merchant did not mean, and every order swept afterwards would be
    filed under the wrong shop.
    """
    if str(site_id or "").strip():
        return await fetch_webflow_site(
            api_token, str(site_id).strip(), timeout=timeout, client=client
        )
    sites = await list_webflow_sites(api_token, timeout=timeout, client=client)
    if len(sites) == 1:
        return sites[0]
    raise WebflowSiteAmbiguousError(
        (
            "this Webflow token reaches no sites"
            if not sites
            else f"this Webflow token reaches {len(sites)} sites; name one as site_id"
        ),
        sites=sites,
    )


async def merge_webflow_credentials(
    *,
    store_id: str,
    updates: Optional[Dict[str, Any]] = None,
    mutate: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    mark_connected: bool = False,
    db: Any = None,
) -> Dict[str, Any]:
    """Read-modify-write this store's credential blob ATOMICALLY, then re-read it.

    The ONE writer of `merchant_stores.api_key` for this platform — connect,
    `webhooks/ensure` and the sweep all come through here. It delegates to the
    shared helper (services/merchant_store_credentials.py) rather than
    hand-copying the critical section: two critical sections over one cell are
    two chances to interleave, and a race proof written against one says nothing
    about the other.

    What is at stake in this blob specifically: `url_secret` is the value baked
    into the webhook URL registered AT WEBFLOW. Losing it to a lost update does
    not merely rotate a secret — it leaves Webflow delivering to a path this
    receiver will 401 forever, and the only repair is a re-provision.
    """
    return await merge_store_credentials(
        store_id=store_id,
        updates=updates,
        mutate=mutate,
        mark_connected=mark_connected,
        db=db,
        parse=parse_webflow_credentials,
        serialize=serialize_webflow_credentials,
    )


def drop_site_scoped_keys(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """Remove every credential and every piece of site-derived state, in place.

    Used by connect when the token now belongs to a DIFFERENT site. It drops the
    whole of `WEBFLOW_SITE_SCOPED_KEYS` rather than a hand-listed subset, which
    is the point: the Squarespace review's finding was a drop list that covered
    the derived state and missed the credential the read path prefers.
    """
    for key in WEBFLOW_SITE_SCOPED_KEYS:
        credentials.pop(key, None)
    return credentials


async def find_webflow_store(store_id: str) -> Optional[Dict[str, Any]]:
    """An ACTIVE Webflow store row by id, or None."""
    from db.database import database

    row = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, name, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'webflow'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": str(store_id or "").strip()},
    )
    return dict(row) if row else None


async def active_webflow_stores() -> List[Dict[str, Any]]:
    """Every active Webflow store. Used by the sweep's all-stores mode."""
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT store_id, merchant_id, domain, name, api_key
        FROM merchant_stores
        WHERE platform = 'webflow'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """
    )
    return [dict(row) for row in rows]
