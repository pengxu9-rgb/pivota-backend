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

# Namespace-prefix tolerant: a sitemap served as <sm:loc> must not read as a
# document containing no URLs at all, which _complete_child_slugs would then
# (correctly, given what it saw) treat as an unreadable child.
_LOC_RE = re.compile(r"<(?:[A-Za-z0-9_.-]+:)?loc>\s*(.*?)\s*</(?:[A-Za-z0-9_.-]+:)?loc>",
                     re.IGNORECASE | re.DOTALL)
_XML_ENTITIES = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"))

# A child sitemap that lists products. Shopify's classic storefronts emit
# `sitemap_products_1.xml`; Hydrogen/headless emit `/sitemap/products/1.xml`.
# Matching only the first form used to send the second down the flat-urlset
# branch, where `/sitemap/products/1.xml` "looks like" a PDP URL and yields the
# slug `1.xml` — i.e. a published set of one garbage entry, against which every
# real product in the store classifies UNPUBLISHED.
_PRODUCT_SITEMAP_RE = re.compile(r"sitemap[_/]products?[_/.]", re.IGNORECASE)


def _is_sitemap_index(xml: str) -> bool:
    return "<sitemapindex" in (xml or "").lower()


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


def _product_slugs_on_host(locs: List[str], expect_host: str) -> Set[str]:
    """PDP slugs from `locs` that actually belong to `expect_host`.

    The host check is what stops us judging one storefront against another's
    sitemap — an apex that serves a DIFFERENT store than the crawled `www.`
    host would otherwise produce a confidently wrong UNPUBLISHED for every row.
    A loc with no host is relative, hence same-host by definition."""
    out: Set[str] = set()
    for loc in locs:
        if "/products/" not in loc:
            continue
        loc_host = normalize_host(urlsplit(loc).hostname or "")
        if loc_host and loc_host != expect_host:
            continue
        slug = normalize_product_slug(loc)
        if slug:
            out.add(slug)
    return out


async def fetch_published_slugs(
    host: str, *, client: Optional[httpx.AsyncClient] = None
) -> Optional[Set[str]]:
    """The set of product handles the merchant publishes to its Online Store.

    Returns None (→ UNKNOWN for every row on this host) whenever the answer
    would be INCOMPLETE rather than merely empty. Incompleteness is the whole
    hazard: a published set that is short by one child sitemap does not look
    wrong, it looks like those products were never published, and the caller
    suppresses them. So every one of these yields None:

    * the index or a child could not be fetched (non-200, timeout, empty body);
    * a child came back 200 but yielded no ``<loc>`` at all — a bot-challenge or
      cookie-interstitial HTML page is exactly this, and is far more likely in
      practice than a clean failure;
    * a child yielded ``<loc>``s but no PDP URLs for this host;
    * a child is itself a nested ``<sitemapindex>`` we do not descend;
    * the root is a ``<sitemapindex>`` whose children we cannot identify as
      product sitemaps (so we never fall through to the flat-urlset reading and
      mistake a child sitemap's own filename for a product handle);
    * either fetch cap is busted.

    `host` is used verbatim for the fetch (only lowercased and trimmed) so a
    store crawled at ``www.`` is judged against ``www.``'s sitemap, not the
    apex's — which may serve an entirely different storefront."""
    fetch_host = (host or "").strip().lower()
    expect_host = normalize_host(fetch_host)
    if not fetch_host or not expect_host:
        return None

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        )
    try:
        base = f"https://{fetch_host}/"
        index = await _get(client, urljoin(base, "sitemap.xml"))
        if not index:
            return None
        entries = _locs(index)
        if not entries:
            return None

        if not _is_sitemap_index(index):
            # A genuinely flat <urlset>. Trust it only if it lists PDPs for us.
            return _product_slugs_on_host(entries, expect_host) or None

        children = [u for u in entries if _PRODUCT_SITEMAP_RE.search(u)]
        if not children or len(children) > MAX_SITEMAP_CHILDREN:
            # An index we cannot navigate is NO ANSWER, never an empty catalog.
            return None

        slugs: Set[str] = set()
        for child in children:
            xml = await _get(client, child if "://" in child else urljoin(base, child))
            if xml is None or _is_sitemap_index(xml):
                return None
            if "<urlset" not in xml.lower():
                # Not a sitemap document at all. A 200 that is really a
                # bot-challenge or cookie-interstitial HTML page lands here, and
                # is the most likely partial read in practice: the index fetch
                # succeeds and a later child gets challenged. Contributing zero
                # slugs would silently mark that child's whole product range
                # UNPUBLISHED, so it has to be no answer for the host.
                return None
            product_locs = [u for u in _locs(xml) if "/products/" in u]
            if not product_locs:
                # A VALID but empty range. Shopify partitions product sitemaps
                # by product-id window (`?from=…&to=…`) and emits an empty
                # <urlset> for a window whose products are all unpublished —
                # biodance.com/sitemap_products_2.xml is exactly this. Treating
                # it as unreadable made every real store with a gap UNKNOWN.
                continue
            got = _product_slugs_on_host(product_locs, expect_host)
            if not got:
                # It listed products, none of them ours: we are reading a
                # different storefront's sitemap. Not an empty catalog.
                return None
            slugs |= got
            if len(slugs) > MAX_SITEMAP_URLS:
                return None
        return slugs or None
    finally:
        if owns_client:
            await client.aclose()


# The ONE cohort field that may assert non-publication without verification.
# Deliberately verbose and unambiguous: an earlier draft called this
# ``sitemap_present``, which collides with services/bd_brand_signals.py's key of
# the same name meaning "this brand's site has a sitemap AT ALL". False there is
# the canonical UNKNOWN case; false here means "suppress this row". Same repo,
# same crawl pipeline, one dict merge away from mass-suppressing a brand without
# a single HTTP request.
PUBLICATION_ASSERTION_KEY = "shopify_published_to_online_store"


def verdict_from_cohort_item(item: Dict[str, object]) -> Optional[str]:
    """Publication verdict carried BY the cohort, if the producer recorded one.

    Returns None — "no answer, go look at the sitemap" — for everything except
    an unambiguous assertion, because this runs BEFORE the network path and
    short-circuits it. Anything that can suppress a row without verification
    belongs behind a field whose only possible meaning is publication.

    ``published_scope`` can therefore only ever say PUBLISHED here. Shopify
    always returns 'web' or 'global' for it, so an unrecognised value means we
    are looking at a field we do not understand, not at an unpublished product
    — the real non-publication marker in Shopify's API is a null ``published_at``.
    Deducing UNPUBLISHED from a scope we failed to parse would be a guess."""
    explicit = item.get(PUBLICATION_ASSERTION_KEY)
    if isinstance(explicit, bool):
        return PUBLISHED if explicit else UNPUBLISHED

    scope = item.get("published_scope")
    if isinstance(scope, str) and scope.strip().lower() in {"web", "global"}:
        return PUBLISHED
    return None


class PublicationOracle:
    """Per-run, per-host cache over :func:`fetch_published_slugs`.

    A cohort is one merchant's whole store, so the sitemap must be fetched once
    per host, not once per row. Hosts that answered None stay cached as None so
    a dead sitemap is not re-fetched 250 times."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client
        self._cache: Dict[str, Optional[Set[str]]] = {}

    async def published_slugs(self, host_or_url: str) -> Optional[Set[str]]:
        """Cached per host AS CRAWLED — `www.brand.com` and `brand.com` are
        fetched separately on purpose. Stripping `www.` here would judge a
        www-crawled store against whatever the apex serves, which is not
        always the same storefront."""
        raw = (host_or_url or "").strip()
        key = (urlsplit(raw).hostname or raw.split("/")[0] or "").strip().lower()
        if not key:
            return None
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
