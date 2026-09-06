#!/usr/bin/env python3
"""Backfill catalog_products.description for brand-official minted canonicals
from the brand's OWN Shopify storefront (body_html), then recompute
pdp_lifecycle_stage and refresh agent_pdp_view.

WHY. The brand-official MINT lane (services/retailer_ingest/brand_official.py →
curated_brand_feed.shopify_product_to_record) carries only product_type as
attribute_summary, so minted rows land with descriptions like "Toner" (< 50
chars) and fail is_candidate_ready — they stay pdp_lifecycle_stage='draft'
forever and never reach serving (catalog_row_trust stays blocked). The
authoritative description ALREADY EXISTS on the same storefront the row was
minted from: /products.json body_html. This backfill joins each row's
canonical_url (https://{domain}/products/{handle}) back to its own storefront
record — brand-authored copy only (ADR-001), never synthesized, never from a
retailer.

Rows whose storefront has no usable body_html (some brands leave it empty) are
left untouched and reported — they stay draft honestly.

--pdp-fallback (OPT-IN) tries one more BRAND-AUTHORED source before giving up on
such a row: the product's own PDP meta description. Measured on jsmbeauty.sg
2026-09-06, 158 of 232 products publish under 50 chars of body_html text while
their PDPs carry 150-340 chars. It costs one merchant-host request per
already-skipped row (plus one per domain for the shop blurb), which is why it is
not the default. Meta copy that is storefront-wide boilerplate rather than this
product's description -- the theme's shop blurb, an app vendor's operational text
-- is dropped by drop_shared_boilerplate and those rows stay blocked. Rows filled
this way are recorded under REFRESH_SOURCE_PDP_META, not the body_html source. Anything still
empty after that routes to the LLM enrichment lane, as before.

Dry-run (default): report per-domain fill/skip counts, write nothing.
    DATABASE_URL=... python3 scripts/backfill_brand_official_descriptions.py

Apply:
    DATABASE_URL=... python3 scripts/backfill_brand_official_descriptions.py --apply

With the PDP fallback (one extra request per empty-body row):
    DATABASE_URL=... python3 scripts/backfill_brand_official_descriptions.py \\
        --domains jsmbeauty.sg --pdp-fallback --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
)
from services.catalog_enrichment_agent.bulk_writer import is_transport_error  # noqa: E402
from services.curated_brand_feed import (  # noqa: E402
    body_html_to_text,
    fetch_pdp_description,
    fetch_shop_description,
    fetch_shopify_products,
)
from services.pdp_lifecycle import compute_lifecycle_stage  # noqa: E402

REFRESH_SOURCE = "brand_official_description_backfill_v1"
# PROVENANCE, recorded rather than left implicit. ADR-001's closing item asks for "provenance
# tagging on each canonical field", and this lane honours it elsewhere (`inci_source`,
# readiness/sources/shopify_live.py's `field_sources`). Before --pdp-fallback,
# catalog_products.description on this lane had exactly ONE possible origin; it now has two, and
# a later reader must be able to tell which filled a given row -- both to audit PDP-meta copy and
# to find it again if the meta-description route is ever judged a lower tier than body copy.
REFRESH_SOURCE_PDP_META = "brand_official_description_backfill_pdp_meta_v1"
MIN_DESC_LEN = 50          # is_candidate_ready's CANDIDATE_DESCRIPTION_MIN_LEN

# Population: Path-C enrichment-minted rows stuck below 'published' whose
# canonical_url points at a Shopify product page. The description source is
# ALWAYS the row's own canonical_url domain — its authoritative storefront.
_SELECT_ROWS = """
    SELECT product_key, content_key, canonical_url, title, image_url,
           description, category_path, tags, demographic, use_case_tags,
           lifestyle_tags, pdp_scope, source_system, source_domain,
           pdp_lifecycle_stage
    FROM catalog_products
    WHERE source_system = 'catalog_enrichment_agent_v1'
      AND sync_status = 'live'
      AND pdp_lifecycle_stage IN ('draft', 'candidate', 'validated')
      AND canonical_url LIKE 'https://%/products/%'
      AND length(coalesce(description, '')) < 50
"""

_UPDATE_ROW = """
    UPDATE catalog_products
       SET description = :description,
           pdp_lifecycle_stage = :stage,
           updated_at = NOW()
     WHERE product_key = :product_key
"""

def handle_from_url(canonical_url: str) -> tuple[str, str]:
    """('domain', 'handle') from https://{domain}/products/{handle}[?…]."""
    parts = urlsplit(str(canonical_url or ""))
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path or ""
    if "/products/" not in path:
        return host, ""
    handle = path.split("/products/", 1)[1].strip("/").split("/")[0]
    return host, handle


def _should_heal(exc: BaseException) -> bool:
    """Transport failure OR the databases-lib poisoned-connection state (a bare
    AssertionError 'Connection is already acquired') — both mean the shared
    connection is unusable and a reconnect can save the rest of the batch."""
    return is_transport_error(exc) or "already acquired" in str(exc)


async def _reconnect() -> None:
    try:
        await database.disconnect()
    except Exception:  # noqa: BLE001
        pass
    for attempt in range(1, 6):
        try:
            await database.connect()
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  (reconnect attempt {attempt} failed: {str(exc)[:100]})")
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("could not re-establish DB connection")


async def _load_body_map(domain: str, max_products: int) -> Dict[str, str]:
    # fetch_shopify_products swallows errors into [] — a transient network
    # failure would silently mark a whole domain "not_in_feed" (4 domains, 292
    # rows on the first prod run). It also returns a PARTIAL list when
    # pagination dies mid-feed, which looks like an exact multiple of the
    # 250-item page size. Retry both shapes before accepting the result.
    products: List[Dict[str, Any]] = []
    for attempt in range(1, 4):
        products = await fetch_shopify_products(domain, max_products=max_products)
        suspect_truncated = (
            products and len(products) < max_products and len(products) % 250 == 0
        )
        if products and not suspect_truncated:
            break
        if attempt < 3:
            shape = "empty" if not products else f"suspiciously page-aligned ({len(products)})"
            print(f"  ({domain}: {shape} feed on attempt {attempt} — retrying)")
            await asyncio.sleep(10 * attempt)
    return {
        str(p.get("handle") or "").strip(): body_html_to_text(p.get("body_html"))
        for p in products
        if isinstance(p, dict) and p.get("handle")
    }


def _norm_copy(value: str) -> str:
    """Compare copy the way a reader would: case- and whitespace-insensitively."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


async def _load_shop_blurb(domain: str, attempts: int = 3) -> Optional[str]:
    """The storefront's blurb, retried like `_load_body_map` and for the same reason.

    `fetch_shop_description` swallows every failure into None, exactly as `fetch_shopify_products`
    does -- and `_load_body_map` already retries 3x because a transient failure there silently
    marks a whole domain 'not_in_feed'. Here the silent failure is in the DANGEROUS direction: an
    unavailable blurb disarms mechanism 1, and the counters would print `boilerplate=0` as though
    nothing had happened. Retry first; `drop_shared_boilerplate` handles a genuine absence.
    """
    for attempt in range(1, attempts + 1):
        blurb = await fetch_shop_description(domain)
        if blurb:
            return blurb
        if attempt < attempts:
            await asyncio.sleep(0.5 * attempt)
    return None


# A candidate whose only content beyond the product's own title is this much or less is a
# title echo, not a description. "Kylie Cosmetics - Glossy Pink Makeup Bag + Deluxe Samples"
# leaves "kylie cosmetics" (15 chars) once the title is removed.
_ECHO_REMAINDER_MIN = 30


def _is_title_echo(value: str, title: Optional[str]) -> bool:
    """Is this value just the product's title with a brand name bolted on?

    THE THIRD BOILERPLATE FAMILY, and the one both other mechanisms are blind to. kyliecosmetics
    .com renders its name tag from a template, `"Kylie Cosmetics - {title}"`. That is PER-PRODUCT,
    so repetition never fires, and it is not the shop blurb, so the comparison never fires -- yet
    it carries zero product information and is admitted or refused purely by whether the title is
    long enough to clear the 50-char floor (2 of 3 accepted values on that storefront; 34 chars
    saved a third by accident). Auto-published as brand-official copy.

    THE THRESHOLD IS A MARGIN, NOT A TUNING. Measured remainders: observed echoes leave 5-15
    characters once the title is removed, observed genuine copy leaves 76-101. 30 sits in that gap
    with room on both sides, and is deliberately NOT raised to catch every template I can imagine
    -- a longer SEO scaffold such as "Buy {title} online at {brand} {country}" leaves 36 and is
    MISSED. That family was not observed in the 2026-09-06 sweep, and fitting the number to a case
    I invented rather than measured is how the previous version of this guard went wrong. Raising
    it also costs real copy: genuine text only just over the 50-char floor leaves roughly
    `50 - len(title)`, so a high threshold starts refusing short descriptions of long-named
    products.
    """
    if not title:
        return False
    v, t = _norm_copy(value), _norm_copy(title)
    if not t or t not in v:
        return False
    remainder = re.sub(r"[^a-z0-9]+", " ", v.replace(t, " ")).strip()
    return len(remainder) < _ECHO_REMAINDER_MIN


def drop_shared_boilerplate(
    candidates: Dict[str, str],
    shop_blurb: Optional[str] = None,
    *,
    handles: Optional[Dict[str, str]] = None,
    titles: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Keep only the PDP meta descriptions that are about ONE product. Pure; no I/O.

    WHY A WHOLE-DOMAIN DECISION. Body copy from /products.json is per-product by construction.
    PDP meta copy is not: several mechanisms put the same string on many PDPs at once, and none of
    them is detectable in a single page's markup.

      1. THE SHOP BLURB. A theme renders og:description as
         `page_description | default: shop.description`, so a product with no SEO description
         serves the storefront's own blurb. Caught by exact comparison against
         `fetch_shop_description`. LOAD-BEARING and not redundant with (2): measured 2026-09-06,
         jsmbeauty.sg's `artist-eyelash-ampoule-serum` served the blurb as the ONLY product in its
         batch to do so, and repetition could not see it.
      2. APP-VENDOR TEXT. Apps write operational strings into the name tag --
         "This product is used for the app BOGOS.io ... Please do not delete/edit it". 231
         characters, over the floor, on 20+ `gwp-*_freegift` products of jsmbeauty.sg. No
         shop-blurb comparison reaches it; repetition does.
      3. A TITLE ECHO from a themed name-tag template -- see `_is_title_echo`. Per-product, not
         the blurb, invisible to both of the above.

    THE REPETITION UNIT IS THE PRODUCT PAGE, NOT THE ROW. Two catalog rows can share one
    canonical_url; they fetch the same PDP and produce the same value, and counting rows would
    drop both as "shared" when it is one product represented twice. `handles` supplies the
    identity to count by.

    AN UNAVAILABLE BLURB DISARMS THE FALLBACK FOR SINGLETONS, rather than letting them through.
    With `shop_blurb=None` mechanism 1 is off, so a value with no sibling has nothing checking it
    at all -- and a homepage 403 would silently re-open exactly the hole this exists to close.

    MEASURED COST, because "the safe direction" is not the same as "free". Over 39 candidates on
    7 storefronts (2026-09-06), 21 were dropped and 4 of those were genuine per-product copy
    shared by a sibling -- 10% of candidates, and 17% on jsmbeauty.sg, the storefront this lane
    exists for. Two eyeshadow palettes share family copy; a merchant copy-pasted one brush kit's
    description onto another, and the second kit's own real copy is lost with it. We take that
    knowingly: a rejected row keeps its description and stays blocked exactly where it was, and
    routes to the LLM enrichment lane, whereas a false ACCEPT auto-publishes boilerplate as
    brand-official copy (`is_published_ready`).

    KNOWN LIMIT, not closed here: the census covers the candidates of THIS run. The population
    query excludes rows already at >= 50 chars, so if another lane fills one member of a shared
    pair, the survivor becomes a singleton in a later run and only mechanism 1 still guards it.
    A stable verdict across runs needs a persisted per-domain ledger of rejected values.
    """
    handles = handles or {}
    titles = titles or {}
    # Count DISTINCT product pages per value, so one product behind two rows is not "shared".
    seen: Dict[str, set] = {}
    for pk, v in candidates.items():
        seen.setdefault(_norm_copy(v), set()).add(handles.get(pk, pk))
    blurb = _norm_copy(shop_blurb)

    kept = {}
    for pk, v in candidates.items():
        n = _norm_copy(v)
        if len(seen[n]) > 1:                       # (2) shared across product pages
            continue
        if blurb and n == blurb:                   # (1) the storefront's own blurb
            continue
        if not blurb:                              # (1) unavailable -> nothing guards a singleton
            continue
        if _is_title_echo(v, titles.get(pk)):      # (3) the title with a brand bolted on
            continue
        kept[pk] = v
    return kept


async def resolve_description(
    body: str,
    domain: str,
    handle: str,
    *,
    pdp_fallback: bool,
    fetch=None,
) -> tuple:
    """Which copy fills this row, and did the PDP supply it? -> (description|None, from_pdp).

    EXTRACTED so the two safety properties are testable at all. They were prose in a docstring,
    and mutation proved that: making the fetch unconditional, or ignoring `--pdp-fallback`, left
    the whole suite green because nothing drove this decision. It decides whether a routine re-run
    quietly starts crawling merchant hosts and prefers meta copy over real body copy, which is too
    load-bearing to leave as a comment inside a loop nothing tests.

    Order matters and is the whole contract: usable body copy wins and costs NO request; the
    fallback is consulted only after body copy has already failed the floor, and only when asked
    for; and the SAME floor applies to what comes back -- a 9-character meta description is how
    these rows got blocked in the first place.
    """
    if len(body or "") >= MIN_DESC_LEN:
        return body, False
    if not pdp_fallback:
        return None, False
    meta = await (fetch or fetch_pdp_description)(domain, handle)
    if meta and len(meta) >= MIN_DESC_LEN:
        return meta, True
    return None, False


async def run(apply: bool, domains_filter: List[str], max_products: int,
              pdp_fallback: bool = False) -> int:
    # initial connect through the same healer as mid-run reconnects —
    # a proxy TLS bad-window at process start must not burn a whole
    # stage attempt (observed 2026-07-17).
    await _reconnect()
    rows: List[Dict[str, Any]] = []
    for attempt in range(1, 4):
        try:
            rows = [dict(r) for r in await database.fetch_all(_SELECT_ROWS)]
            break
        except Exception as exc:  # noqa: BLE001
            if not _should_heal(exc) or attempt == 3:
                raise
            print(f"  (population fetch attempt {attempt} failed: {str(exc)[:100]} — reconnecting)")
            await _reconnect()
    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    foreign_domain = 0
    for r in rows:
        host, handle = handle_from_url(r["canonical_url"])
        if not host or not handle:
            continue
        # Provenance guard: fill ONLY from the row's own brand storefront. The
        # mint lane stamps source_domain == canonical host; a Gemini-lane row
        # whose canonical_url points at a Shopify RETAILER (or whose
        # source_domain is NULL — the known validated.jsonl gap) is skipped —
        # retailer copy must never become a brand-official description.
        if host != str(r.get("source_domain") or "").strip().lower():
            foreign_domain += 1
            continue
        if domains_filter and host not in domains_filter:
            continue
        r["_handle"] = handle
        by_domain.setdefault(host, []).append(r)
    if foreign_domain:
        print(f"[provenance] {foreign_domain} row(s) skipped: canonical_url host != source_domain")

    print(f"[population] {sum(len(v) for v in by_domain.values())} rows "
          f"across {len(by_domain)} domain(s)  (apply={apply})")

    totals = {"filled": 0, "published": 0, "validated": 0, "no_body": 0, "from_pdp": 0,
              "not_in_feed": 0, "boilerplate": 0, "blurb_unavailable": 0,
              "update_failed": 0, "refresh_failed": 0}
    # content_key -> did ANY row behind it come from PDP meta. A flat list cannot carry this:
    # the refresh runs in its own loop below, and an earlier version read a LEAKED `r` from the
    # fill loop there, stamping every key with the last row of the last domain's provenance.
    touched_cks: Dict[str, bool] = {}

    for domain in sorted(by_domain):
        drows = by_domain[domain]
        body_map = await _load_body_map(domain, max_products)
        filled = no_body = not_in_feed = from_pdp = boilerplate = 0

        # PASS 1 — DECIDE, writing nothing. Body copy could be written as it is resolved, but PDP
        # meta copy cannot: whether a value is this product's description or storefront-wide
        # boilerplate is only answerable once every candidate for the domain is in hand
        # (drop_shared_boilerplate). So both take the same two-pass route.
        resolved: Dict[str, str] = {}    # product_key -> body copy
        candidates: Dict[str, str] = {}  # product_key -> PDP meta copy, still suspect
        # A TRUNCATED FEED MAKES THE CENSUS NON-DETERMINISTIC. `drop_shared_boilerplate` votes on
        # the candidates it is given, so a storefront split by --max-products is judged in halves:
        # measured on jsmbeauty.sg 2026-09-06, the same 24 candidates kept 13 as one batch and 15
        # as two, and the string that survived the split was a shared one. Rather than write a
        # verdict that depends on the cut, refuse the fallback for this domain and say so.
        domain_pdp_fallback = pdp_fallback
        if pdp_fallback and len(body_map) >= max_products:
            print(f"  WARN {domain}: feed hit the --max-products cap ({max_products}) — the "
                  f"boilerplate census would cover only part of the storefront, so the PDP "
                  f"fallback is skipped here. Re-run with a higher --max-products.")
            domain_pdp_fallback = False
        for r in drows:
            body = body_map.get(r["_handle"])
            if body is None:
                not_in_feed += 1
                continue
            body, from_pdp_row = await resolve_description(
                body, domain, r["_handle"], pdp_fallback=domain_pdp_fallback
            )
            if body is None:
                no_body += 1
                continue
            (candidates if from_pdp_row else resolved)[str(r["product_key"])] = body

        if candidates:
            blurb = await _load_shop_blurb(domain)
            if not blurb:
                # Not a warning to skim past: with mechanism 1 off, every singleton candidate is
                # dropped below, so this line explains a sudden `boilerplate` spike.
                print(f"  WARN {domain}: shop blurb unavailable — the blurb comparison is "
                      f"disarmed and unrepeated candidates will be refused")
                totals["blurb_unavailable"] += 1
            kept = drop_shared_boilerplate(
                candidates, blurb,
                handles={str(r["product_key"]): r["_handle"] for r in drows},
                titles={str(r["product_key"]): r.get("title") for r in drows},
            )
            boilerplate = len(candidates) - len(kept)
            totals["boilerplate"] += boilerplate
            candidates = kept

        # PASS 2 — WRITE.
        for r in drows:
            pk = str(r["product_key"])
            from_pdp_row = pk in candidates
            body = candidates.get(pk) if from_pdp_row else resolved.get(pk)
            if body is None:
                continue
            if from_pdp_row:
                from_pdp += 1
            new_row = {**r, "description": body}
            new_stage = compute_lifecycle_stage(new_row)
            totals[new_stage] = totals.get(new_stage, 0) + 1
            filled += 1
            if apply:
                for attempt in (1, 2):
                    try:
                        await database.execute(_UPDATE_ROW, {
                            "description": body,
                            "stage": new_stage,
                            "product_key": r["product_key"],
                        })
                        if r.get("content_key"):
                            ck_w = str(r["content_key"])
                            # Several product_keys can share one content_key. If ANY contributing
                            # row was filled from PDP meta, the view's copy may be meta copy, so
                            # the marker has to say so — OR, never overwrite.
                            touched_cks[ck_w] = touched_cks.get(ck_w, False) or from_pdp_row
                        break
                    except Exception as exc:  # noqa: BLE001 — heal a poisoned
                        # connection and retry once; a bad row must not abort
                        # the domain (this is the primary write path).
                        if attempt == 1 and _should_heal(exc):
                            await _reconnect()
                            continue
                        totals["update_failed"] += 1
                        print(f"  WARN update failed pk={r['product_key']}: "
                              f"{type(exc).__name__}: {str(exc)[:150]}")
                        break
        totals["filled"] += filled
        totals["no_body"] += no_body
        totals["from_pdp"] = totals.get("from_pdp", 0) + from_pdp
        totals["not_in_feed"] += not_in_feed
        print(f"  {domain:22s} rows={len(drows):4d} feed={len(body_map):4d} "
              f"fill={filled:4d} empty_body={no_body:3d} not_in_feed={not_in_feed:3d} "
              f"from_pdp={from_pdp:3d} boilerplate={boilerplate:3d}")

    if apply and touched_cks:
        distinct = sorted(touched_cks)
        print(f"[view] refreshing {len(distinct)} content_key view(s) ...")
        for ck in distinct:
            for attempt in (1, 2):
                try:
                    await refresh_agent_pdp_view_for_content_key(
                        ck,
                        refresh_source=(REFRESH_SOURCE_PDP_META if touched_cks[ck]
                                       else REFRESH_SOURCE),
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — a transport failure
                    # poisons the pinned connection ("Connection is already
                    # acquired" cascade, 511 refreshes lost on the first prod
                    # run) — heal it and retry once before skipping the key.
                    if attempt == 1 and _should_heal(exc):
                        await _reconnect()
                        continue
                    totals["refresh_failed"] += 1
                    print(f"  WARN view refresh failed for {ck}: {str(exc)[:120]}")
                    break

    await database.disconnect()
    print(f"[done] {totals}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write updates (default: dry-run)")
    p.add_argument("--domains", default="", help="comma-separated domain allowlist")
    p.add_argument("--max-products", type=int, default=800)
    # OPT-IN. It costs one PDP fetch per row whose body_html already failed, against a merchant
    # host, so it must be a choice rather than something a routine re-run starts doing.
    p.add_argument("--pdp-fallback", action="store_true",
                   help="when body_html is empty, take the PDP's own meta description")
    args = p.parse_args()
    domains = [d.strip().lower() for d in args.domains.split(",") if d.strip()]
    return asyncio.run(run(args.apply, domains, args.max_products, args.pdp_fallback))


if __name__ == "__main__":
    raise SystemExit(main())
