"""An affiliate network's product datafeed as a retailer ingest source.

For retailers that refuse crawlers but publish through affiliate networks. Measured 2026-09-23:
global.oliveyoung.com and us.oliveyoung.com answer 403 "Access restricted" to robots.txt,
/products.json and /.well-known/ucp, while Olive Young runs affiliate programs (Involve Asia for
Global/SEA, Rakuten Advertising for the US store). Scraping around an explicit block is not an
option; the partner feed is.

A feed row becomes the SAME record the Shopify lane builds (curated_brand_feed.shopify_product_to_record),
so everything downstream -- plan, guards, detectors, the ledger, approval, the apply gate, readback --
is reused unchanged. Two things differ, both enforced here:
  * canonical_url is the retailer's own product page (listing identity: see
    ingestion._LISTING_QUERY_KEYS for stores that name products in the query);
  * destination_url is the network's tracking link, accepted ONLY on hosts the job declares
    (`link_hosts`) or the retailer's own host, and only over https. It is what a buyer clicks.
  * the variant ids are the network's SKUs, which services.variant_identity cannot place as a
    storefront's own (UNVERIFIABLE), so no cart is ever built from them. The primary readiness rule
    that demands a merchant-issued variant for every ext:retailer: product is excused ONLY for a
    product every one of whose offers is stamped `validated_at = "affiliate_feed:<network>"`.

NO GUESSED COLUMN NAMES. Each network's real feed header is only known after approval, so every job
declares its mapping (`fields`); a mapped column missing from the feed refuses the run.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

# The record fields a feed can supply, and which of them a mapping must name.
FIELDS = ("id", "title", "brand", "product_url", "link", "price", "currency", "gtin", "image",
          "description", "category", "availability", "parent_id", "variant_title", "sku")
REQUIRED_FIELDS = ("id", "title", "brand", "product_url", "link", "price", "currency")
MAX_ROWS = 200_000
MAX_FEED_BYTES = 200 * 1024 * 1024

_TRUE = {"1", "true", "yes", "y", "in stock", "instock", "in_stock", "available", "t"}
_FALSE = {"0", "false", "no", "n", "out of stock", "outofstock", "out_of_stock", "unavailable", "sold out", "f"}


class FeedError(ValueError):
    """The feed or its mapping cannot be trusted; the stage refuses rather than guessing."""


def validate_feed_options(feed: Any) -> Dict[str, Any]:
    """The `options.feed` block. Raises FeedError. No secret ever lives here: `url_env` NAMES the
    environment variable (a mounted secret) holding the feed URL, which usually embeds a token."""
    if not isinstance(feed, dict):
        raise FeedError("options.feed must be an object")
    allowed = {"network", "url_env", "format", "fields", "link_hosts", "retailer_host", "json_path"}
    unknown = set(feed) - allowed
    if unknown:
        raise FeedError(f"unknown options.feed keys {sorted(unknown)}")
    for key in ("network", "url_env", "format", "retailer_host"):
        if not isinstance(feed.get(key), str) or not feed[key].strip():
            raise FeedError(f"options.feed.{key} is required")
    # The prefix keeps an enqueuer from pointing a job at some OTHER secret the drain job holds.
    if not re.fullmatch(r"AFFILIATE_FEED_[A-Z0-9_]{2,48}", feed["url_env"]):
        raise FeedError("options.feed.url_env must name an AFFILIATE_FEED_* environment variable")
    if feed["format"] not in {"csv", "tsv", "json"}:
        raise FeedError("options.feed.format must be csv, tsv or json")
    fields = feed.get("fields")
    if not isinstance(fields, dict) or not all(isinstance(v, str) and v.strip() for v in fields.values()):
        raise FeedError("options.feed.fields must map record fields to feed column names")
    bad = set(fields) - set(FIELDS)
    if bad:
        raise FeedError(f"options.feed.fields has unknown record fields {sorted(bad)}")
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    if missing:
        raise FeedError(f"options.feed.fields must map {missing}")
    hosts = feed.get("link_hosts") or []
    if not isinstance(hosts, list) or not all(isinstance(h, str) and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,63}", h)
                                              for h in hosts):
        raise FeedError("options.feed.link_hosts must be a list of hostnames")
    return feed


def parse_feed(text: str, *, fmt: str, json_path: Optional[str] = None) -> List[Dict[str, str]]:
    """Rows as {column: string}. CSV/TSV by header; JSON as a list, or the list at `json_path`
    (dot-separated, e.g. "data.products")."""
    if len(text) > MAX_FEED_BYTES:
        raise FeedError("feed exceeds the size cap")
    text = text.removeprefix("\ufeff")  # a UTF-8 BOM would otherwise rename the first column
    if fmt in {"csv", "tsv"}:
        # HTML descriptions routinely exceed the csv module's 128 KiB default field limit.
        csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
        reader = csv.DictReader(io.StringIO(text), delimiter="\t" if fmt == "tsv" else ",")
        try:
            rows = [{k.strip(): (v or "").strip() for k, v in row.items() if isinstance(k, str) and k}
                    for row in reader]
        except csv.Error as exc:
            raise FeedError(f"feed is not valid {fmt}: {exc}") from exc
    else:
        try:
            data: Any = json.loads(text)
        except ValueError as exc:
            raise FeedError(f"feed is not valid JSON: {exc}") from exc
        for part in (json_path or "").split("."):
            if part:
                if not isinstance(data, dict) or part not in data:
                    raise FeedError(f"json_path {json_path!r} not found in the feed")
                data = data[part]
        if not isinstance(data, list):
            raise FeedError("the JSON feed (at json_path) is not a list of products")
        rows = [{str(k): ("" if v is None else str(v)).strip() for k, v in row.items()}
                for row in data if isinstance(row, dict)]
    if len(rows) > MAX_ROWS:
        raise FeedError(f"feed has {len(rows)} rows; the cap is {MAX_ROWS}")
    return rows


_UNSAFE_URL_CHAR = re.compile(r"[\\\s\x00-\x1f\x7f]")


def _https_host(url: str) -> Optional[str]:
    """The host of a plain https URL, or None. Refuses anything a browser could read differently
    from urlsplit: a backslash ("https://evil.example\\.allowed.com" goes to evil.example),
    whitespace, control characters, userinfo, a port, or a host outside [a-z0-9.-]."""
    if not isinstance(url, str) or _UNSAFE_URL_CHAR.search(url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username or parts.password or port
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)):
        return None
    return host.removeprefix("www.")


def _availability(value: str) -> Optional[bool]:
    v = value.strip().lower()
    return True if v in _TRUE else False if v in _FALSE else None


def feed_rows_to_records(rows: List[Dict[str, str]], feed: Dict[str, Any], *, vendors: List[str],
                         category_path: str, currency: str) -> List[Dict[str, Any]]:
    """Map feed rows (grouped into products by parent_id, else id) to curated retailer records."""
    from services.curated_brand_feed import shopify_product_to_record

    fields: Dict[str, str] = feed["fields"]
    header = set(rows[0]) if rows else set()
    absent = sorted(col for col in fields.values() if col not in header)
    if rows and absent:
        raise FeedError(f"mapped columns missing from the feed header: {absent}")
    from services.catalog_enrichment_agent.ingestion import retailer_listing_identity

    host = feed["retailer_host"].strip().lower().removeprefix("www.")
    link_hosts = {h.lower().removeprefix("www.") for h in (feed.get("link_hosts") or [])} | {host}
    wanted = {" ".join(v.casefold().split()) for v in vendors}
    network = str(feed.get("network") or "unknown")
    seen_ids: set = set()

    def col(row: Dict[str, str], field: str) -> str:
        name = fields.get(field)
        return row.get(name, "").strip() if name else ""

    products: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        brand = col(row, "brand")
        if " ".join(brand.casefold().split()) not in wanted:
            continue
        row_currency = col(row, "currency").upper()
        if row_currency != currency:
            raise FeedError(f"feed row {col(row, 'id')!r} is priced in {row_currency or 'nothing'}, not {currency}")
        row_id = col(row, "id")
        if not row_id or row_id in seen_ids:
            raise FeedError(f"feed row id {row_id!r} is blank or repeated")
        seen_ids.add(row_id)
        product_url, link = col(row, "product_url"), col(row, "link")
        # EXACTLY the retailer's host: a subdomain page would pass here and crash the listing identity.
        if _https_host(product_url) != host:
            raise FeedError(f"feed row {row_id!r}: product_url is not an https page on {host}")
        try:
            retailer_listing_identity(host, product_url)
        except ValueError as exc:
            raise FeedError(f"feed row {row_id!r}: {exc}") from exc
        link_host = _https_host(link)
        if not link_host or not any(link_host == h or link_host.endswith("." + h) for h in link_hosts):
            raise FeedError(f"feed row {row_id!r}: link host is not in options.feed.link_hosts")
        # Separate namespaces, so a row whose id equals another row's parent_id cannot merge into it.
        key = f"p:{col(row, 'parent_id')}" if col(row, "parent_id") else f"i:{row_id}"
        product = products.setdefault(key, {
            "id": key, "title": col(row, "title"), "vendor": brand, "handle": _handle(key),
            "product_type": col(row, "category"), "body_html": col(row, "description"),
            "images": [{"src": col(row, "image")}] if col(row, "image") else [],
            "variants": [], "_product_url": product_url, "_link": link,
        })
        # One product, one listing, one click. A group that disagrees is refused, never first-row-wins.
        if (product["_product_url"], product["_link"], " ".join(product["vendor"].casefold().split())) != (
                product_url, link, " ".join(brand.casefold().split())):
            raise FeedError(f"feed rows under {key!r} disagree on product_url, link or brand")
        product["variants"].append({
            # Namespaced: a network SKU is never a storefront's variant id, even when it is all digits
            # (services.variant_identity would class 8+ digits as merchant-issued and a cart could be
            # built from it). This keeps every feed id UNVERIFIABLE.
            "id": f"feed:{network}:{row_id}", "title": col(row, "variant_title") or "Default Title",
            "price": col(row, "price"), "sku": col(row, "sku") or None, "barcode": col(row, "gtin") or None,
            "available": _availability(col(row, "availability")),
        })

    records: List[Dict[str, Any]] = []
    for product in products.values():
        product_url, link = product.pop("_product_url"), product.pop("_link")
        record = shopify_product_to_record(product, domain=host, category_path=category_path,
                                           brand_override=product["vendor"], emit_native_variants=True,
                                           currency=currency, source_role="retailer",
                                           retailer_name=host)
        if not record:
            continue
        for offer in record.get("offers") or []:
            # The listing is the retailer's page; the click is the network's tracking link.
            offer["canonical_url"] = product_url
            offer["destination_url"] = link
            # Provenance, where the Shopify lane writes "shopify_products_json". It is also the
            # marker primary_ingestion.inspect_primary_plan reads to excuse the native-variant rule.
            offer["validated_at"] = feed_provenance(feed)
        records.append(record)
    return records


FEED_PROVENANCE_PREFIX = "affiliate_feed:"


def feed_provenance(feed: Dict[str, Any]) -> str:
    return FEED_PROVENANCE_PREFIX + str(feed.get("network") or "unknown")


def _handle(key: str) -> str:
    """A stable, URL-safe stand-in for Shopify's handle (the record builder requires one)."""
    return re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-") or "item"


MAX_REDIRECTS = 5


async def fetch_feed_text(feed: Dict[str, Any], *, env: Dict[str, str], timeout_s: float = 120.0) -> str:
    """Download the feed from the URL in the named environment variable. The URL is never logged,
    returned or put in an error: it usually embeds the publisher token. Every hop must be https,
    and the body is streamed against MAX_FEED_BYTES instead of being buffered first."""
    import httpx

    url = (env.get(feed["url_env"]) or "").strip()
    if not _https_host(url):
        raise FeedError(f"{feed['url_env']} is not set to an https feed URL in this job's environment")
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout_s) as client:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = str(resp.url.join(resp.headers.get("location", "")))
                    if not _https_host(url):
                        raise FeedError("feed download redirected off https")
                    continue
                if resp.status_code != 200:
                    raise FeedError(f"feed download answered HTTP {resp.status_code}")
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
                    raise FeedError("feed exceeds the size cap")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_FEED_BYTES:
                        raise FeedError("feed exceeds the size cap")
                try:
                    return bytes(body).decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise FeedError("feed is not UTF-8") from exc
    raise FeedError(f"feed download redirected more than {MAX_REDIRECTS} times")
