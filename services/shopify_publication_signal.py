"""Is a crawled Shopify URL a PUBLISHED storefront product, or an ad landing page?

The question this answers
-------------------------
Shopify merchants routinely mint one "product" per ad variant so each creative
gets its own landing page and its own pixel. Those variants are real rows in
``/products.json`` — same brand, same title, same images as the product they
advertise — but they are NOT part of the merchant's published catalog. The
external_brand_crawl seeder cannot tell them apart from the real PDP, so they
land as catalog products, mint their own ``product_key`` (hence their own
public PDP), and ride the shared ``content_key``'s serving eligibility. See
pengxu9-rgb/PIVOTA-Agent#1926.

Why the signal is the SITEMAP and not the URL string
----------------------------------------------------
Two string-shaped rules were measured and rejected before this module existed:

* "the slug shares no tokens with the title" — 403 flags over 11,192 rows, only
  ~146 of them campaign pages (~36% precision). It mostly flagged Japanese
  catalogs, where an ASCII tokenizer cannot match ``cicaserum`` to
  ``シカナイアシンアミドACカーミングセラム40ml``.
* a per-brand URL regex — a hardcoded literal that generalises to nothing, and
  which was already proven incomplete (it matched ``_cm_`` but not the ``_cjm_``
  and ``_bs_`` families, a 1.7x undercount).

``sitemap_products_*.xml`` is the merchant's OWN statement of which products are
published to the Online Store channel. It is brand-agnostic, language-agnostic,
needs no tokenizer, and campaign landing pages are excluded from it by Shopify
because they are not published to that channel. Measured against prod's live
crawl cohort (2026-08-07, 34 hosts):

    ================  ======  ========  ==========================================
    host              live    in sitemap  note
    ================  ======  ========  ==========================================
    biodance.com          37       0%   every live row is a campaign page
    holikaholika.com      59     100%
    iunik.com             45     100%
    rovectin.com          44     100%
    lador.us              49     100%
    easydew.jp            37     100%   ← the catalogs the token rule destroyed
    toun28.jp             26     100%
    mealit.jp             13     100%
    dermafirm.jp           8     100%
    todaywith.jp           4     100%
    cellfusionc.jp        35      97%
    paulmitchell.com     250      96%
    celimax.jp            60      78%   misses are Qoo10/Rakuten referral pages
    arencia.jp            63      27%   misses are ``-copy`` duplicate listings
    ================  ======  ========  ==========================================

THE ENCODING TRAP (this cost a full measurement pass — do not undo it)
----------------------------------------------------------------------
Shopify emits sitemap ``<loc>`` slugs PERCENT-ENCODED, while the crawl stores
``destination_url`` already decoded. Compare them raw and every non-ASCII
catalog reads as 0% published: cellfusionc.jp measured 3% before the fix and
97% after, todaywith.jp 0% then 100%. :func:`normalize_product_slug` decodes,
NFC-normalises, and casefolds both sides. A comparison that skips this step
reproduces exactly the failure mode that got the token heuristic rejected.

Fail-open by construction
-------------------------
Every uncertainty returns :data:`UNKNOWN`, never :data:`UNPUBLISHED`: no
sitemap, an unreadable sitemap, a PARTIALLY read sitemap (one child fetch
failing would otherwise mark real products unpublished), a non-``/products/``
URL, or a store larger than the fetch caps. Absence is only asserted when the
merchant's own complete sitemap was read and the slug is not in it.
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from services.brand_claim_service import normalize_host

# Verdicts. Deliberately plain strings so they log and serialise as themselves.
PUBLISHED = "published"
UNPUBLISHED = "unpublished"
UNKNOWN = "unknown"

USER_AGENT = "pivota-catalog-crawler/1.0 (+publication check)"

# One sitemap child is ~50k URLs at Shopify's cap; a handful of children covers
# any store we crawl. These caps bound a pathological store rather than express
# a policy — busting one yields UNKNOWN (fail open), never UNPUBLISHED.
MAX_SITEMAP_CHILDREN = 25
MAX_SITEMAP_URLS = 100_000

FETCH_TIMEOUT_S = float(os.getenv("SHOPIFY_SITEMAP_TIMEOUT_SECONDS", "20") or 20)
FETCH_ATTEMPTS = 3

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_XML_ENTITIES = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"))


def _unescape(text: str) -> str:
    for entity, char in _XML_ENTITIES:
        text = text.replace(entity, char)
    return text


def normalize_product_slug(url: Optional[str]) -> str:
    """Comparable form of a Shopify product handle taken from a PDP URL.

    Percent-DECODES, NFC-normalises, then casefolds. Both sides of every
    membership test must go through this — see THE ENCODING TRAP in the module
    docstring. Returns '' when the URL is absent or is not a ``/products/`` URL,
    which callers must treat as UNKNOWN rather than as a miss."""
    if not url or not isinstance(url, str):
        return ""
    path = urlsplit(url.strip()).path or url.strip()
    if "/products/" not in path:
        return ""
    slug = path.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        return ""
    return unicodedata.normalize("NFC", unquote(slug)).casefold()


def _locs(xml: str) -> List[str]:
    return [_unescape(m) for m in _LOC_RE.findall(xml or "")]


async def _get(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """GET with retries. None on any failure — the caller turns that into
    UNKNOWN. Never raises: a merchant's flaky sitemap host must not abort an
    onboarding run."""
    for attempt in range(FETCH_ATTEMPTS):
        try:
            resp = await client.get(url)
            if resp.status_code == 200 and resp.text:
                return resp.text
            # 4xx is a definitive "no such sitemap"; don't burn retries on it.
            if 400 <= resp.status_code < 500:
                return None
        except Exception:
            pass
        if attempt + 1 < FETCH_ATTEMPTS:
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def fetch_published_slugs(
    host: str, *, client: Optional[httpx.AsyncClient] = None
) -> Optional[Set[str]]:
    """The set of product handles the merchant publishes to its Online Store.

    Returns None (→ UNKNOWN for every row on this host) when the sitemap is
    missing, unreadable, or only PARTIALLY readable. Partial is the dangerous
    case: a child fetch that fails after two children succeeded would silently
    shrink the published set and mark real products unpublished, so it is
    treated as no answer at all."""
    host = normalize_host(host)
    if not host:
        return None

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        )
    try:
        base = f"https://{host}/"
        index = await _get(client, urljoin(base, "sitemap.xml"))
        if not index or "<loc>" not in index.lower():
            return None

        entries = _locs(index)
        children = [u for u in entries if "sitemap_products" in u.lower()]
        if not children:
            # A flat urlset (no index). Only trust it if it actually lists PDPs.
            flat = {normalize_product_slug(u) for u in entries if "/products/" in u}
            flat.discard("")
            return flat or None

        if len(children) > MAX_SITEMAP_CHILDREN:
            return None

        slugs: Set[str] = set()
        for child in children:
            xml = await _get(client, child if "://" in child else urljoin(base, child))
            if xml is None:
                return None  # partial read — see the docstring
            for loc in _locs(xml):
                if "/products/" not in loc:
                    continue
                slug = normalize_product_slug(loc)
                if slug:
                    slugs.add(slug)
                if len(slugs) > MAX_SITEMAP_URLS:
                    return None
        return slugs or None
    finally:
        if owns_client:
            await client.aclose()


def verdict_from_cohort_item(item: Dict[str, object]) -> Optional[str]:
    """Publication verdict carried BY the cohort, if the crawler recorded one.

    The durable fix is upstream: whatever produces the cohort JSON should record
    Shopify's own ``published_scope`` (and/or an explicit ``sitemap_present``)
    so this module never has to go back over the network. Until it does, this
    returns None and the caller falls back to the sitemap fetch. Honouring the
    field now means the crawler can start emitting it without a second change
    here."""
    explicit = item.get("sitemap_present")
    if isinstance(explicit, bool):
        return PUBLISHED if explicit else UNPUBLISHED

    scope = item.get("published_scope")
    if isinstance(scope, str) and scope.strip():
        # Shopify: 'web' = published to the Online Store, 'global' = all
        # channels. Anything else (notably the empty/absent scope campaign
        # products carry) means it is not on the storefront channel.
        return PUBLISHED if scope.strip().lower() in {"web", "global"} else UNPUBLISHED
    return None


class PublicationOracle:
    """Per-run, per-host cache over :func:`fetch_published_slugs`.

    A cohort is one merchant's whole store, so the sitemap must be fetched once
    per host, not once per row. Hosts that answered None stay cached as None so
    a dead sitemap is not re-fetched 250 times."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client
        self._cache: Dict[str, Optional[Set[str]]] = {}

    async def published_slugs(self, host: str) -> Optional[Set[str]]:
        key = normalize_host(host)
        if key not in self._cache:
            self._cache[key] = await fetch_published_slugs(key, client=self._client)
        return self._cache[key]

    async def classify(self, item: Dict[str, object]) -> str:
        """PUBLISHED / UNPUBLISHED / UNKNOWN for one cohort item."""
        carried = verdict_from_cohort_item(item)
        if carried is not None:
            return carried

        url = item.get("destination_url")
        slug = normalize_product_slug(url if isinstance(url, str) else None)
        if not slug:
            return UNKNOWN

        slugs = await self.published_slugs(url if isinstance(url, str) else "")
        if slugs is None:
            return UNKNOWN
        return PUBLISHED if slug in slugs else UNPUBLISHED

    async def classify_all(self, cohort: Iterable[Dict[str, object]]) -> List[str]:
        return [await self.classify(item) for item in cohort]
