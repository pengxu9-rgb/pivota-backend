"""Paced Shopify storefront reads with an explicitly declared market."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from services.crawl_politeness import CrawlPaced, RobotsDisallowed


class MarketsRefused(Exception):
    """The capture ends without a plan: `outcome`/`status`/`reason` go on the run and the job, and
    `checks` records the evidence read so far. `transient` = a 429/5xx: retry the stage later."""

    def __init__(self, outcome: str, status: str, reason: str, *, checks: Optional[Dict[str, Any]] = None,
                 transient: bool = False):
        super().__init__(reason)
        self.outcome, self.status, self.reason = outcome, status, reason
        self.checks, self.transient = checks, transient


def _host(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower()
    host = (urlsplit(raw if "//" in raw else f"https://{raw}").hostname or "").rstrip(".")
    return host.removeprefix("www.")


def _ua() -> str:
    from services.curated_brand_feed import _UA
    return _UA


def _is_json(resp: Any) -> bool:
    """A JSON-shaped answer: application/json, or Shopify's own `text/javascript` (2026-10-11).

    Shopify serves its AJAX endpoints -- `/cart.js` and `/products/<handle>.js` -- as
    `text/javascript; charset=utf-8` with a JSON body. Requiring "json" in the content type refused every
    storefront as a bot wall at the first `/cart.js` read (the session proof), so the capture never produced
    one offer in prod (0 shopify_markets_* siblings; the tests' fakes answered application/json, which
    Shopify does not send). JavaScript-typed answers are accepted HERE only as a
    candidate: `ShopifyStorefrontSession.json` still requires the body to parse to a JSON object or array, so a JS challenge
    page is refused exactly like the HTML wall it stands in for."""
    ctype = str(resp.headers.get("content-type") or "").lower()
    return "json" in ctype or "javascript" in ctype


class ShopifyStorefrontSession:
    """Requests against one storefront host -- cookie-less pinned reads, or the fallback's cookie session --
    every request paced through the crawl's gate,
    every answer classified, the final host of every redirect chain checked."""

    def __init__(self, client: Any, host: str, polite: Any, evidence: Dict[str, Any],
                 market: str):
        self.client, self.host, self.polite, self.evidence = client, host, polite, evidence
        self.market = market
        self.requests = 0

    def _refuse(self, outcome: str, status: str, reason: str, *, transient: bool = False) -> MarketsRefused:
        return MarketsRefused(outcome, status, reason, transient=transient)

    async def request(self, method: str, path: str, *, expect_json: bool, **kw: Any) -> Any:
        """The response, unless it is a block (unverifiable), a throttle (transient) or a redirect to
        another host; a 200 that should be JSON and is not is a bot wall. Other codes are the caller's."""
        url = f"https://{self.host}{path}"
        # Shopify Markets capture (/localization, /cart.js) is Shopify by construction: pace it on the
        # crawl IP's shared Shopify-edge budget (a no-op with CRAWL_SHOPIFY_EDGE_PACER_ENABLED off).
        from services import shopify_edge_pacer
        shopify_edge_pacer.mark_shopify_host(self.host)
        try:
            await self.polite.before_request(url, user_agent=_ua(), max_wait=0)
        except RobotsDisallowed as exc:
            raise self._refuse("robots_disallowed", "nothing", _robots_refusal(path, self.host, self.market)) from exc
        except CrawlPaced as exc:  # CrawlDelayTooLong: the host asks for less than we can
            raise self._refuse("crawl_paced", "failed", f"{path}: {exc}") from exc
        import httpx
        try:
            self.requests += 1
            resp = await self.client.request(method, url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise self._refuse("crawl_throttled", "queued", f"{path}: {type(exc).__name__}", transient=True) from exc
        self.polite.note_response(url, resp.status_code, retry_after=resp.headers.get("retry-after"),
                                  headers=resp.headers)
        final = _host(str(getattr(resp, "url", "") or url))
        if final != _host(self.host):
            # A geo-router or a sibling regional store: another catalog, another currency.
            refusal = self._refuse("capture_unverifiable", "failed",
                                   f"{path}: the storefront redirected to {final or '(unknown)'}; a regional "
                                   f"store is a different store")
            refusal.off_host = final or "(unknown)"
            raise refusal
        code = resp.status_code
        if code == 429 or code >= 500:
            raise self._refuse("crawl_throttled", "queued", f"HTTP {code} at {path}", transient=True)
        if code in (401, 403):
            raise self._refuse("capture_unverifiable", "failed",
                               f"HTTP {code} at {path}: a block is unverifiable; never worked around")
        if code == 200 and expect_json and not _is_json(resp):
            raise self._refuse("capture_unverifiable", "failed",
                               f"{path} answered {resp.headers.get('content-type') or 'no content type'}, not "
                               f"JSON (a bot wall reads exactly like this); unverifiable")
        return resp

    async def json(self, path: str) -> Tuple[int, Any]:
        """(status, parsed body); the body is None for any status but 200."""
        resp = await self.request("GET", path, expect_json=True)
        if resp.status_code != 200:
            return resp.status_code, None
        return 200, self._body(resp, path)

    async def json_presented(self, path: str) -> Tuple[int, Any, Optional[str], Optional[str]]:
        """(status, parsed body, currency, problem): `json`, plus the currency THIS response was presented in,
        from its own `cart_currency` Set-Cookie (shopify_presentment.presentment_currency: exactly one
        three-letter value, else `problem`)."""
        from services.shopify_presentment import presentment_currency
        resp = await self.request("GET", path, expect_json=True)
        currency, problem = presentment_currency(resp.headers.get_list("set-cookie"))
        if resp.status_code != 200:
            return resp.status_code, None, currency, problem
        return 200, self._body(resp, path), currency, problem

    def _body(self, resp: Any, path: str) -> Any:
        try:
            body = resp.json()
        except ValueError:
            raise self._refuse("capture_unverifiable", "failed", f"{path} did not parse as JSON; unverifiable")
        if not isinstance(body, (dict, list)):
            # A JS-typed answer that is a bare string or number is no storefront JSON: a challenge or a stub.
            raise self._refuse("capture_unverifiable", "failed",
                               f"{path} answered {type(body).__name__}, not a JSON object; unverifiable")
        return body

    async def cart_currency(self) -> Optional[str]:
        _code, cart = await self.json("/cart.js")
        currency = cart.get("currency") if isinstance(cart, dict) else None
        return currency if isinstance(currency, str) else None


def _robots_refusal(path: str, host: str, market: str) -> str:
    """The recorded reason for a capture refused by robots.txt: `robots_disallowed:<path>` first, so the
    ledger names exactly which path the store forbids."""
    return (f"robots_disallowed:{path} -- {host}'s robots.txt disallows it for our user agent; the capture "
            f"obeys robots.txt and never works around it, so no {market} offer is captured")
