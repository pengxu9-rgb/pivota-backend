"""Find US brand product candidates before the existing offer evidence workflow."""

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
import os
import re
import sys
import time
from unicodedata import category, normalize
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

from .capture import capture


PROJECT = "pivota-staging"
JOB_PREFIX = "meitu-us-official-rediscovery-"
MAX_SITEMAPS = 6
MAX_RESULTS = 3
MAX_XML = 3_000_000
MAX_VARIANT_SAMPLES = 24
MAX_VARIANT_PAGE_BYTES = 20_000
MAX_SEARCH_RESULTS = 8
SHOPIFY_AJAX_VARIANT_LIMIT = 250
HEADERS = {
    "User-Agent": "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}
HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,20}\Z")
HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,219}\Z", re.I)
SHOPIFY_PRODUCT_PATH = re.compile(r"(?P<locale>/(?:en|en-us|us|usa))?/products/(?P<handle>[a-z0-9][a-z0-9-]{0,219})/?\Z", re.I)
SHOPIFY_COLLECTION_PRODUCT = re.compile(
    r"(?P<locale>/(?:en|en-us|us|usa))?/collections/[a-z0-9][a-z0-9-]{0,219}"
    r"/products/(?P<handle>[a-z0-9][a-z0-9-]{0,219})/?\Z", re.I,
)
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
LOCALE = re.compile(r"/([a-z]{2}(?:-[a-z]{2})?)(?=/|$)", re.I)
US_LOCALES = {"en", "en-us", "us", "usa"}
US_PRODUCT_PREFIX = {"www.charlottetilbury.com": "/us/product/", "www.lordandberry.com": "/usa/",
                     "www.clarinsusa.com": "/en/"}
VARIANT_TERMS = {"mini", "kit", "duo", "trio", "bundle", "set", "liquid", "metallic", "refill", "liner"}
GENERIC = {"the", "a", "and", "lip", "lipstick", "gloss", "color", "colour", "lipgloss", "matte", "lipliner"}
BRAND_VENDOR_ALIASES = (
    {"M.A.C", "MAC Cosmetics"}, {"Tarte", "Tarte Cosmetics"},
    {"Stila", "Stila Cosmetics"}, {"Surratt", "Surratt Beauty"},
    {"Flower Beauty", "Flower"},
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


def fetch(url, limit):
    request = Request(url, headers=HEADERS)
    opener = build_opener(NoRedirect)
    try:
        response = opener.open(request, timeout=12)
    except HTTPError as error:
        delay = error.headers.get("Retry-After", "4")
        if error.code != 429 or not delay.isascii() or not delay.isdecimal() or len(delay) > 2 or int(delay) > 15:
            raise
        time.sleep(max(4, int(delay)))
        response = opener.open(request, timeout=12)
    with response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("unexpected_http_response")
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("response_too_large")
        return response.headers, body


def probe(fetcher, url, limit):
    try:
        return 200, fetcher(url, limit)[1]
    except HTTPError as error:
        return error.code, b""
    except (OSError, URLError, ValueError):
        return None, b""


def same_market_url(raw, host):
    if not isinstance(raw, str) or len(raw) > 1200:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    locale = LOCALE.match(parts.path)
    if parts.scheme != "https" or parts.netloc != host or parts.fragment or not parts.path.startswith("/"):
        return None
    if locale and locale[1].casefold() not in US_LOCALES:
        return None
    return parts


def product_path(path):
    return bool(re.fullmatch(r"/(?:(?:en|en-us|us|usa)/)?products?/[^/]+/?", path, re.I)
                or path.lower().endswith(".html")
                or re.match(r"/(?:usa/|catalog/product/view/)", path, re.I))


def product_url(raw, host, *, approved_anchor=False):
    parts = same_market_url(raw, host)
    if parts is None or not parts.path.casefold().startswith(US_PRODUCT_PREFIX.get(host, "/")):
        return None
    collection = SHOPIFY_COLLECTION_PRODUCT.fullmatch(parts.path)
    if collection:
        path = f"{collection['locale'] or ''}/products/{collection['handle']}"
        if path.casefold().startswith(US_PRODUCT_PREFIX.get(host, "/")):
            parts = parts._replace(path=path)
    if SHOPIFY_PRODUCT_PATH.fullmatch(parts.path):
        parts = parts._replace(path=parts.path.rstrip("/"))
    if product_path(parts.path) or (approved_anchor and parts.path != "/"):
        return parts._replace(query="")
    return None


def words(value):
    text = "".join(c for c in normalize("NFKD", value.casefold()) if category(c) != "Mn")
    return re.findall(r"[^\W_]+", text.replace("lipgloss", "lip gloss").replace("lipcolour", "lip colour"))


def match_score(query, title):
    query, title = words(query), words(title)
    if not query or not title:
        return 0
    if query == title:
        return 4
    if not (set(query) - GENERIC) or (set(title) - set(query)) & VARIANT_TERMS:
        return 0
    if f" {' '.join(query)} " in f" {' '.join(title)} ":
        return 3
    # Rule A (current line counts): every DISTINCTIVE word of the line name is in the title. Filler and generic
    # words ("the", "lip", "lipstick", "gloss"...) need not be; the variant-term guard above still rejects a title
    # that adds a bundle, size or format word the query did not name.
    if set(query) - GENERIC <= set(title):
        return 2
    return 0


def search(host, name, fetcher):
    query = urlencode({"q": name, "resources[type]": "product", "resources[limit]": MAX_SEARCH_RESULTS, "country": "US"})
    status, body = probe(fetcher, f"https://{host}/search/suggest.json?{query}", 700_000)
    try:
        products = json.loads(body)["resources"]["results"]["products"]
        if not isinstance(products, list):
            return status, [], 0, "invalid_search_payload"
    except (ValueError, TypeError, KeyError, RecursionError):
        return status, [], 0, "invalid_search_payload" if status == 200 else "search_unavailable"
    results = []
    for product in products[:MAX_SEARCH_RESULTS]:
        if not isinstance(product, dict):
            continue
        handle, title = product.get("handle"), product.get("title")
        if isinstance(title, str) and isinstance(handle, str) and HANDLE.fullmatch(handle):
            results.append((f"https://{host}/products/{handle}", title[:300]))
    return status, results, len(products), None


def fingerprint(values):
    return sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def sitemap(host, fetcher, offset=0, prefix_sha256=None):
    pending = deque([f"https://{host}/sitemap.xml"])
    seen, products, statuses, prefix_receipts = {}, set(), [], []
    while pending:
        url = pending.popleft()
        if url in seen:
            continue
        index = len(seen)
        if index == offset + MAX_SITEMAPS:
            pending.appendleft(url)
            break
        if index == offset and prefix_sha256 and fingerprint(list(seen.items())) != prefix_sha256:
            return [], [], {"status": "restart_required", "offset": offset, "next_offset": None}
        current = index >= offset
        status, body = probe(fetcher, url, MAX_XML)
        linked_urls = []
        receipt = {"url": url, "http": status}
        (statuses if current else prefix_receipts).append(receipt)
        if index == 0 and status in {301, 302, 404}:
            robots_url = f"https://{host}/robots.txt"
            robots_status, robots = probe(fetcher, robots_url, 160_000)
            if current:
                statuses.append({"url": robots_url, "http": robots_status,
                                 **({"failure": "robots_unavailable"} if robots_status != 200 else {})})
            for raw in re.findall(r"^sitemap:\s*(\S+)\s*$", robots.decode("utf-8", errors="replace"), flags=re.I | re.M):
                link = same_market_url(raw, host)
                if link and link.path.casefold().endswith(".xml"):
                    pending.append(link.geturl())
                    linked_urls.append(link.geturl())
        seen[url] = fingerprint([status, linked_urls])
        if status != 200:
            if not linked_urls:
                receipt["failure"] = "sitemap_unavailable"
            continue
        if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", body, re.I):
            receipt["failure"] = "unsupported_sitemap_document"
            continue
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            receipt["failure"] = "invalid_sitemap_xml"
            continue
        kind = root.tag.rsplit("}", 1)[-1]
        if kind not in {"urlset", "sitemapindex"}:
            receipt["failure"] = "unsupported_sitemap_document"
            continue
        for node in root.findall("./*/{*}loc"):
            link = same_market_url(node.text, host)
            if link is None:
                continue
            if kind == "urlset":
                product = product_url(link.geturl(), host)
                if product:
                    linked_urls.append(product.geturl())
                    if current:
                        products.add(product.geturl())
            path = link.path.casefold()
            if kind == "sitemapindex" and path.endswith(".xml"):
                pending.append(link.geturl())
                linked_urls.append(link.geturl())
        seen[url] = fingerprint([status, linked_urls])
    if offset and (len(seen) < offset or (len(seen) == offset and fingerprint(list(seen.items())) != prefix_sha256)):
        return [], [], {"status": "restart_required", "offset": offset, "next_offset": None}
    deferred = list(dict.fromkeys(url for url in pending if url not in seen))
    prefix_failures = [row for row in prefix_receipts if row.get("failure")]
    failures = prefix_failures + [row for row in statuses if row.get("failure")]
    sitemap_state = ("partial" if deferred else "blocked" if any(row["http"] in {403, 429} for row in failures)
                     else "incomplete" if failures else "exhausted")
    coverage = {"status": sitemap_state, "offset": offset, "failed_source_count": len(failures),
                "pages_examined": len(seen) - offset, "prefix_failed_source_count": len(prefix_failures),
                "deferred_sitemap_count": len(deferred),
                "deferred_sitemap_sample": deferred[:3], "next_offset": len(seen) if deferred else None,
                "next_prefix_sha256": fingerprint(list(seen.items())) if deferred else None}
    return prefix_failures[:MAX_SITEMAPS] + statuses, sorted(products), coverage


def offer_evidence(url, fetcher):
    path = urlsplit(url).path
    shopify_path = SHOPIFY_PRODUCT_PATH.fullmatch(path)
    kinds = ["public_pdp"]
    if shopify_path:
        kinds.insert(0, "shopify_ajax")
    product_body, observed_countries = None, set()

    def fetch_product(source, limit):
        nonlocal product_body
        headers, product_body = fetcher(source, limit)
        cookies = (headers.get_all("Set-Cookie", ()) if hasattr(headers, "get_all")
                   else [v for k, v in headers.items() if k.casefold() == "set-cookie"])
        for cookie in cookies:
            key, _, value = cookie.split(";", 1)[0].strip().partition("=")
            if key.casefold() in {"localization", "country", "_shopify_country"}:
                code = unquote(value.strip('"')).upper()
                if re.fullmatch(r"[A-Z]{2}", code):
                    observed_countries.add(code)
        return headers, product_body

    attempts = []
    for kind in kinds:
        try:
            evidence = capture({"url": url, "source_kind": kind}, fetch_product if kind == "shopify_ajax" else fetcher)
            status = evidence["status"]
        except HTTPError as error:
            evidence = {"status": "source_http_unavailable", "http": error.code}
            status = evidence["status"]
        except (OSError, URLError, ValueError):
            evidence = {"status": "source_transport_unavailable"}
            status = evidence["status"]
        attempts.append({"source_kind": kind, **{k: evidence[k] for k in ("status", "http", "reason") if k in evidence}})
        if status == "shopify_us_quote_observed":
            if observed_countries - {"US"}:
                attempts[-1]["status"] = "market_conflict"
                return None, {"status": "market_conflict", "market_evidence": "conflicting_storefront_country",
                              "observed_countries": sorted(observed_countries)}, attempts
            handle = json.loads(product_body).get("handle")
            return kind, {**evidence, "market_evidence": "native_usd_presentment",
                          "storefront_country_proof": "US" if observed_countries else None,
                          "handle": handle if isinstance(handle, str) and HANDLE.fullmatch(handle) else None,
                          "requested_handle": shopify_path["handle"]}, attempts
        if status == "public_product_metadata_observed":
            currencies = evidence["explicit_offer_currencies"]
            if currencies and "USD" not in currencies:
                return None, {**evidence, "market_evidence": "foreign_currency_only"}, attempts
            return kind, {**evidence, "market_evidence": "public_usd_metadata" if "USD" in currencies else "identity_only"}, attempts
        if evidence.get("http") in {403, 429}:
            break
    return None, evidence, attempts


def brand_key(value):
    return "".join(words(value)) if isinstance(value, str) else ""


def shopify_identity(evidence, row):
    if not evidence["handle"]:
        return "handle_unverified"
    if evidence["handle"] != evidence["requested_handle"]:
        return "handle_mismatch"
    expected = brand_key(row["brand"])
    accepted = {expected, *(brand_key(alias) for alias in row.get("brand_aliases", []))}
    for family in BRAND_VENDOR_ALIASES:
        normalized = {brand_key(alias) for alias in family}
        if expected in normalized:
            accepted.update(normalized)
    vendor = brand_key(evidence.get("vendor"))
    if not vendor:
        return "vendor_unverified"
    return "verified" if vendor in accepted else "vendor_mismatch"


def reviewable_variant(variant):
    return bool(variant["gtin"] and variant["in_stock"] is True and (variant.get("sku") or "").strip())


def variant_page(evidence, cursor):
    variants = sorted(evidence["variants"], key=lambda v: (not reviewable_variant(v), int(v["variant_id"])))
    identity = fingerprint([(v["variant_id"], v["gtin"], v["sku"], v["title"], v["price_minor"], v["in_stock"])
                            for v in variants])
    offset = cursor.get("offset", 0)
    stale = bool(cursor) and (cursor["sha256"] != identity or offset > len(variants))
    page, page_bytes = [], 0
    if not stale:
        for variant in variants[offset:offset + MAX_VARIANT_SAMPLES]:
            page_bytes += len(json.dumps(variant, ensure_ascii=False, separators=(",", ":")).encode()) + 1
            if page_bytes > MAX_VARIANT_PAGE_BYTES:
                break
            page.append(variant)
    stop = offset + len(page)
    eligible = sum(reviewable_variant(v) for v in variants)
    upstream_limited = evidence.get("source_variant_count", len(variants)) >= SHOPIFY_AJAX_VARIANT_LIMIT
    return {**evidence, "observed_variant_count": len(variants),
            "in_stock_gtin_variant_count": sum(bool(v["gtin"] and v["in_stock"]) for v in variants),
            "in_stock_gtin_sku_variant_count": eligible, "variants": page,
            "variant_coverage": {"status": ("restart_required" if stale else "partial" if stop < len(variants)
                                            else "incomplete" if upstream_limited else "exhausted"),
                                 "source_variant_limit_reached": upstream_limited,
                                 "offset": offset, "retained_count": len(page),
                                 "deferred_count": len(variants) if stale else len(variants) - stop,
                                 "deferred_reviewable_count": eligible if stale else max(0, eligible - stop),
                                 "next_offset": stop if not stale and stop < len(variants) else None,
                                 "sha256": identity}}


def scoped_candidates(row, fetcher, get_sitemap, get_evidence):
    host = row["host"]
    name = row["name"]
    queries = list(dict.fromkeys([name, *row["search_names"]]))
    cursor = row.get("cursor", {})
    sitemap_offset = cursor.get("sitemap_offset", 0)
    candidate_offset = cursor.get("candidate_offset", 0)
    variant_cursors = row.get("variant_cursors", {})
    retry_urls = set(row.get("retry_pdp_urls", []))
    candidates, search_log = {}, []

    def add(raw, title, method):
        parts = product_url(raw, host)
        if parts is None:
            return
        url = parts.geturl()
        original = match_score(name, title)
        related = max(match_score(term, title) for term in queries)
        if related == 0:
            return
        score = original + 5 if original else related
        old = candidates.get(url)
        if old is None or score > old["score"] or (
            score == old["score"] and method == "shopify_site_search" and old["found_via"] != method
        ):
            candidates[url] = {"title_hint": title, "url": url, "found_via": method, "score": score}

    def verify(item):
        kind, evidence, attempts = get_evidence(item["url"])
        title = evidence.get("title")
        identity = None
        if kind == "shopify_ajax":
            identity = shopify_identity(evidence, row)
            evidence = {**variant_page(evidence, variant_cursors.get(item["url"], {})), "identity_status": identity}
        if identity not in {None, "verified"}:
            signal = "shopify_identity_needs_review"
        elif kind and title:
            original = match_score(name, title)
            named = any(match_score(term, title) for term in queries)
            signal = ("original_title_candidate" if original == 4 else "related_title_candidate" if named
                      else "tracker_link_needs_title_review" if item["found_via"] == "tracker_official_url"
                      else "extracted_title_differs_from_discovery")
        else:
            signal = "product_evidence_unavailable"
        return {**item, "status": signal, "source_kind": kind, "evidence": evidence, "attempts": attempts}

    raw_anchor = row.get("anchor_url")
    anchor = product_url(raw_anchor, host, approved_anchor=True)
    anchor_url = anchor.geturl() if anchor else None
    anchor_outcome = None
    admitted = []
    if anchor_url:
        slug = unquote(anchor.path.rstrip("/").rsplit("/", 1)[-1])
        title_hint = slug.removesuffix(".html").replace("-", " ")
        pinned = verify({"title_hint": title_hint, "url": anchor_url, "found_via": "tracker_official_url",
                         "score": match_score(name, title_hint)})
        admitted.append(pinned)
        anchor_outcome = {"requested_url": raw_anchor, "url": anchor_url, "status": pinned["status"]}
    elif raw_anchor:
        anchor_outcome = {"requested_url": raw_anchor, "status": "anchor_url_rejected"}

    if not sitemap_offset and row["platform"] in {"shopify", "unknown"}:
        for term in queries:
            status, hits, source_count, failure = search(host, term, fetcher)
            search_log.append({"name": term, "http": status, "returned": len(hits), "source_count": source_count,
                               "suggestion_limit_reached": source_count >= MAX_SEARCH_RESULTS,
                               **({"failure": failure} if failure else {})})
            for url, title in hits:
                add(url, title, "shopify_site_search")
    sitemap_log, urls, sitemap_coverage = get_sitemap()
    for url in urls:
        slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
        add(url, slug.removesuffix(".html").replace("-", " "), "official_sitemap")
    candidates.pop(anchor_url, None)
    ranked = sorted(candidates.values(), key=lambda hit: (-hit["score"], hit["url"]))
    candidate_sha256 = fingerprint([(hit["url"], hit["title_hint"], hit["found_via"]) for hit in ranked])
    stale = sitemap_coverage["status"] == "restart_required" or bool(candidate_offset) and (
        cursor["candidate_sha256"] != candidate_sha256 or candidate_offset >= len(ranked)
    )
    search_limited = cursor.get("search_suggestion_limit_reached", False) or any(
        item["suggestion_limit_reached"] for item in search_log
    )
    search_failure = cursor.get("search_failure") if sitemap_offset else next(
        ({"http": item["http"], "failure": item["failure"]} for item in search_log if item.get("failure")), None
    )
    next_cursor = None
    page = [] if stale else ranked[candidate_offset:candidate_offset + MAX_RESULTS - len(admitted)]
    admitted.extend(verify(hit) for hit in page)
    attempted_urls = {item["url"] for item in admitted}
    for url in sorted(variant_cursors.keys() - attempted_urls):
        admitted.append(verify({"url": url, "title_hint": name, "found_via": "variant_continuation", "score": 0}))
    stop = candidate_offset + len(page)
    if not stale and stop < len(ranked):
        next_cursor = {**cursor, "candidate_offset": stop, "candidate_sha256": candidate_sha256}
    elif not stale and sitemap_coverage["next_offset"] is not None:
        next_cursor = {"sitemap_offset": sitemap_coverage["next_offset"],
                       "sitemap_prefix_sha256": sitemap_coverage["next_prefix_sha256"]}
        if search_limited:
            next_cursor["search_suggestion_limit_reached"] = True
        if search_failure:
            next_cursor["search_failure"] = search_failure
    if not next_cursor and not variant_cursors and not stale:
        attempted_urls = {item["url"] for item in admitted}
        for url in sorted(retry_urls - attempted_urls)[:MAX_RESULTS]:
            admitted.append(verify({"url": url, "title_hint": name, "found_via": "source_retry", "score": 0}))
    variant_pages = {item["url"]: item["evidence"]["variant_coverage"] for item in admitted
                     if "variant_coverage" in item["evidence"]}
    pending_variants = {url: {"offset": page["next_offset"], "sha256": page["sha256"]}
                        for url, page in variant_pages.items() if page["next_offset"] is not None}
    stale_variants = [url for url, page in variant_pages.items() if page["status"] == "restart_required"]
    variant_limit = any(page["source_variant_limit_reached"] for page in variant_pages.values())
    source_failures = [item for item in search_log + sitemap_log if item.get("failure")]
    if sitemap_offset and search_failure:
        source_failures.append(search_failure)
    for item in admitted:
        failures = [attempt for attempt in item["attempts"]
                    if attempt.get("http") in {401, 403, 408, 429} or attempt.get("http", 0) >= 500
                    or attempt.get("status") == "source_transport_unavailable"]
        if failures and not item["source_kind"]:
            retry_urls.add(item["url"])
            source_failures.extend(failures)
        else:
            retry_urls.discard(item["url"])
    coverage_state = ("restart_required" if stale or stale_variants else "partial" if next_cursor or pending_variants
                      else "blocked" if any(item.get("http") in {403, 429} for item in source_failures)
                      else "incomplete" if source_failures or search_limited or retry_urls or variant_limit else "exhausted")
    coverage = {"status": coverage_state,
                "candidate_restart_required": stale,
                "page_cursor": cursor, "next_cursor": next_cursor, "candidate_offset": candidate_offset,
                "discovered_candidate_count": len(ranked) + bool(anchor_url), "attempted_candidate_count": len(admitted),
                "deferred_candidate_count": len(ranked) if stale else len(ranked) - stop,
                "deferred_candidate_sample": [hit["url"] for hit in ranked[0 if stale else stop:][:3]],
                "pending_variant_pages": pending_variants, "variant_restart_urls": stale_variants,
                "source_variant_limit_reached": variant_limit, "retry_pdp_urls": sorted(retry_urls),
                "search_retry_required": search_failure is not None,
                "search_suggestion_limit_reached": search_limited, "failed_source_count": len(source_failures),
                "sitemap": sitemap_coverage}
    if anchor_outcome is not None:
        coverage["anchor_outcome"] = anchor_outcome
    return {"search": search_log, "sitemaps": sitemap_log, "candidate_count": len(admitted),
            "candidates": admitted, "coverage": coverage}


def checked_queries(payload):
    batch = payload.get("batch_id", "")
    rows = payload.get("queries")
    if (payload.get("schema_version"), payload.get("market")) != (1, "US") or not re.fullmatch(r"[a-z0-9-]{8,75}", batch):
        raise ValueError("invalid US discovery batch")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("empty or oversized batch")
    lines, ids = set(), set()
    for row in rows:
        host, names = row["host"], row["search_names"]
        anchor = row.get("anchor_url")
        aliases = row.get("brand_aliases", [])
        if (type(row["line"]) is not int or not 1 <= row["line"] <= 10_000
                or not all(isinstance(row[key], str) and 0 < len(row[key]) <= 200 for key in ("brand", "meitu_id", "name"))
                or row["platform"] not in {"shopify", "other", "unknown"}
                or host is not None and (not isinstance(host, str) or not HOST.fullmatch(host) or ".." in host)
                or anchor is not None and (not isinstance(anchor, str) or len(anchor) > 1200)
                or not isinstance(names, list) or len(names) > 3
                or not all(isinstance(t, str) and 0 < len(t) <= 160 for t in names)
                or not isinstance(aliases, list) or len(aliases) > 8
                or not all(isinstance(alias, str) and 0 < len(alias) <= 160 for alias in aliases)):
            raise ValueError("invalid product query")
        check_cursors(row)
        if row["line"] in lines or row["meitu_id"] in ids:
            raise ValueError("duplicate Meitu identity")
        lines.add(row["line"])
        ids.add(row["meitu_id"])
    return sorted(rows, key=lambda row: row["line"])


def is_digest(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def check_cursors(row):
    cursor = row.get("cursor", {})
    if not isinstance(cursor, dict) or cursor.keys() - {
        "sitemap_offset", "sitemap_prefix_sha256", "candidate_offset", "candidate_sha256", "search_suggestion_limit_reached",
        "search_failure",
    } or type(cursor.get("search_suggestion_limit_reached", False)) is not bool:
        raise ValueError("invalid discovery continuation cursor")
    search_failure = cursor.get("search_failure")
    if search_failure is not None and (
        not isinstance(search_failure, dict) or search_failure.keys() != {"http", "failure"}
        or search_failure["http"] is not None and type(search_failure["http"]) is not int
        or not isinstance(search_failure["failure"], str) or len(search_failure["failure"]) > 100
    ):
        raise ValueError("invalid search continuation status")
    for offset_key, digest_key in (("sitemap_offset", "sitemap_prefix_sha256"), ("candidate_offset", "candidate_sha256")):
        offset = cursor.get(offset_key, 0)
        digest = cursor.get(digest_key)
        if type(offset) is not int or offset < 0 or (offset > 0 and not is_digest(digest)) or (offset == 0 and digest is not None):
            raise ValueError("invalid discovery continuation offset or fingerprint")
    variant_cursors = row.get("variant_cursors", {})
    if not isinstance(variant_cursors, dict) or len(variant_cursors) > 2 * MAX_RESULTS:
        raise ValueError("invalid variant continuation map")
    for url, cursor in variant_cursors.items():
        parts = product_url(url, row["host"])
        if (parts is None or parts.geturl() != url or not isinstance(cursor, dict)
                or cursor.keys() != {"offset", "sha256"} or type(cursor["offset"]) is not int
                or cursor["offset"] < 0 or not is_digest(cursor["sha256"])):
            raise ValueError("invalid variant continuation")
    review_urls = row.get("retry_pdp_urls", [])
    if (not isinstance(review_urls, list) or not all(isinstance(url, str) for url in review_urls)
            or len(review_urls) != len(set(review_urls))):
        raise ValueError("invalid product retry list")
    for url in review_urls:
        parts = product_url(url, row["host"], approved_anchor=url == row.get("anchor_url"))
        if parts is None or parts.geturl() != url:
            raise ValueError("invalid product retry URL")


def run(payload, fetcher=fetch):
    rows = checked_queries(payload)
    batch = payload["batch_id"]
    grouped = {}
    for row in rows:
        grouped.setdefault(row["host"], []).append(row)

    def work(entry):
        host, group = entry
        responses = []
        last_request = 0.0

        def paced_fetch(url, limit):
            nonlocal last_request
            if fetcher is fetch:
                time.sleep(max(0, last_request + 0.8 - time.monotonic()))
                last_request = time.monotonic()
            return fetcher(url, limit)

        get_sitemap = lru_cache(maxsize=1)(lambda offset, prefix: sitemap(host, paced_fetch, offset, prefix))
        get_evidence = lru_cache(maxsize=6)(lambda url: offer_evidence(url, paced_fetch))
        for row in group:
            base = {"scope": "meitu_official_rediscovery_query", "batch_id": batch, "market": "US",
                    "meitu_line": row["line"], "meitu_id": row["meitu_id"], "brand": row["brand"],
                    "name": row["name"], "host": host}
            if host is None:
                coverage = {"status": "host_unverified", "next_cursor": None}
                if row.get("anchor_url"):
                    coverage["anchor_outcome"] = {"requested_url": row["anchor_url"], "status": "anchor_url_rejected"}
                responses.append({**base, "status": "official_host_needs_evidence", "candidate_count": 0, "candidates": [],
                                  "coverage": coverage})
                continue
            cursor = row.get("cursor", {})
            details = scoped_candidates(row, paced_fetch,
                lambda: get_sitemap(cursor.get("sitemap_offset", 0), cursor.get("sitemap_prefix_sha256")), get_evidence)
            match = any(item["status"] in {"original_title_candidate", "related_title_candidate"}
                        for item in details["candidates"])
            statuses = [p["http"] for p in details["search"] + details["sitemaps"]]
            statuses += [p.get("http") for item in details["candidates"] for p in item["attempts"]]
            status = ("candidate_observed" if match else "source_rate_limited" if 429 in statuses
                      else "source_access_held" if 403 in statuses else "no_product_evidence_in_scan")
            responses.append({**base, **details, "status": status})
        return responses

    with ThreadPoolExecutor(max_workers=5) as pool:
        records = sorted((row for group in pool.map(work, grouped.items()) for row in group), key=lambda row: row["meitu_line"])
    candidates = [item for row in records for item in row["candidates"]]
    observed = {item["url"]: item for item in candidates
                if item["status"] in {"original_title_candidate", "related_title_candidate"}}
    native = [item["evidence"] for item in observed.values() if item["source_kind"] == "shopify_ajax"]
    return records, {"scope": "meitu_official_rediscovery_summary", "market": "US", "batch_id": batch,
                     "observed_at": datetime.now(timezone.utc).isoformat(), "query_count": len(records),
                     "brands": len({row["brand"] for row in records}), "hosts_scanned": len(grouped) - (None in grouped),
                     "query_status": dict(Counter(row["status"] for row in records)),
                     "query_coverage_status": dict(Counter(row["coverage"]["status"] for row in records)),
                     "candidate_urls_with_evidence": len(observed), "native_shopify_us_urls": len(native),
                     "native_shopify_variants": sum(item["observed_variant_count"] for item in native),
                     "commerce_index_writes": 0, "merchant_db_writes": 0, "offer_db_writes": 0}


def main():
    cloud_job = os.getenv("CLOUD_RUN_JOB")
    if cloud_job and (not cloud_job.startswith(JOB_PREFIX) or os.getenv("GOOGLE_CLOUD_PROJECT") != PROJECT
                      or os.getenv("DATABASE_URL")):
        raise ValueError("unexpected read-only discovery runtime")
    rows, summary = run(json.load(sys.stdin))
    for row in [*rows, summary]:
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
