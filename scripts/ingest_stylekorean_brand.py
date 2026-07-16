"""StyleKorean single-brand intake door — crawl one brand, match each SKU to our
canonical catalog, and produce an attach/mint PLAN. Dry-run by default.

    # dry-run: crawl COSRX, emit the plan, write nothing
    DATABASE_URL=<prod-proxy> python3 scripts/ingest_stylekorean_brand.py --brand cosrx --out /tmp/cosrx_plan.jsonl

    # apply the ATTACH lane only (retailer offers on matched canonicals):
    DATABASE_URL=<prod-proxy> python3 scripts/ingest_stylekorean_brand.py --brand cosrx --apply-offers

    # dry-run the MINT lane too (fetch brand-official catalog, predict resolution):
    DATABASE_URL=<prod-proxy> python3 scripts/ingest_stylekorean_brand.py --brand cosrx --brand-official-domain cosrx.com

    # apply MINT: ingest brand-official canonicals + attach SK offers to them:
    DATABASE_URL=<prod-proxy> python3 scripts/ingest_stylekorean_brand.py \
        --brand cosrx --brand-official-domain cosrx.com --apply-mint --apply-offers

LANES:
  ATTACH — a `retailer_match_key` (size/pack/promo-normalized) match to an
           existing canonical → attach a StyleKorean US offer via the sanctioned
           scripts/attach_retailer_offer path. Written by --apply-offers.
  MINT   — no match → brand-official-first. Fetch the brand's own Shopify catalog
           (authentic US copy + GTIN), ingest it as the canonical anchor, then the
           MINT SKU attaches to brand-direct content (ADR-001) — never minted from
           StyleKorean's retailer copy. SKUs with no brand-official match are
           RESIDUE (flagged, not minted). Written by --apply-mint (+ --apply-offers
           to also attach the SK offers onto the new canonicals).

Nothing writes without an explicit --apply-* flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from scripts.attach_retailer_offer import (  # noqa: E402
    attach_retailer_offer,
    build_retailer_offer_row,
)
from services.pdp_matcher.retailer_match import (  # noqa: E402
    build_match_index,
    match_record,
    retailer_match_key,
)
from services.retailer_ingest import brand_official as bo  # noqa: E402
from services.retailer_ingest import stylekorean as sk  # noqa: E402
from services.retailer_ingest.sitemap_crawler import crawl_products  # noqa: E402


def _brand_tokens(records: List[Dict[str, Any]]) -> List[str]:
    return sorted({str(r.get("brand")).strip() for r in records if r.get("brand")})


def _write_residue_candidates(path: str, items: List[Dict[str, Any]], *, domain: str, category_path: str) -> None:
    """Emit residue SKUs as catalog_enrichment_agent candidates so they hand off to
    the existing Gemini PDP-resolution + Path-C ingest
    (scripts/run_catalog_enrichment.py) — the fuzzy/LLM lane for brand-official
    names that deterministic matching can't safely bridge (line-name/version drift)."""
    # FLAT shape — the runner + gemini_url_validator.validate_candidate read
    # brand/product_name at the TOP level (like every other category candidates
    # file). The nested {"pdp": {...}} form silently sends empty-brand prompts
    # to Gemini (caught in residue batch 1, 2026-07-16).
    with open(path, "w") as f:
        for p in items:
            cand = {
                "brand": p.get("brand"),
                "product_name": p.get("title"),
                "category_path": category_path,
                "attribute_summary": (p.get("record", {}).get("pdp", {}) or {}).get("attribute_summary", ""),
                "source_domain": domain,
                "expected_url_domains": [domain],
            }
            f.write(json.dumps(cand, ensure_ascii=False, default=str) + "\n")


def _offer_eligible(p: Dict[str, Any]) -> bool:
    """A plan item may become an offer only with a positive USD price. A "0"/
    negative price or a non-USD currency (crawler landed on a non-Global locale)
    must never reach attach_retailer_offer — it would stamp estimated_best_price
    + price_confidence=0.9 with garbage under market=US."""
    try:
        price = float(p.get("price"))
    except (TypeError, ValueError):
        return False
    if price <= 0:
        return False
    currency = (p.get("currency") or "").strip().upper()
    return currency in ("", "USD")


def _pick_offer_per_product(items: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
    """One offer slot exists per (product_key, merchant) — attach_retailer_offer's
    offer_id hashes exactly that — so N retailer listings matching one canonical
    (size variants, Double-Duo bundles) would otherwise UPSERT over each other
    last-write-wins in crawl-completion order: nondeterministic, and a bundle
    price can land on a single item. Pick deterministically instead: in-stock
    first, then lowest price (base item beats bundle), then shortest title, then
    URL as a stable tiebreak. Returns (winners, collapsed_count)."""
    by_pk: Dict[str, List[Dict[str, Any]]] = {}
    for p in items:
        if p.get("matched_product_key") and _offer_eligible(p):
            by_pk.setdefault(p["matched_product_key"], []).append(p)
    winners: List[Dict[str, Any]] = []
    collapsed = 0
    for pk, group in by_pk.items():
        group.sort(key=lambda p: (
            0 if p["record"]["offers"][0]["availability"] == "in_stock" else 1,
            float(p["price"]),
            len(str(p.get("title") or "")),
            str(p.get("url") or ""),
        ))
        winners.append(group[0])
        collapsed += len(group) - 1
    return winners, collapsed


async def _attach_offer_items(items: List[Dict[str, Any]]) -> tuple[int, int]:
    """Attach one deterministic StyleKorean offer per matched canonical.
    Returns (applied, skipped) where skipped = ineligible (no/zero/non-USD
    price) + collapsed size/bundle variants."""
    eligible_missing = sum(
        1 for p in items if p.get("matched_product_key") and not _offer_eligible(p)
    )
    winners, collapsed = _pick_offer_per_product(items)
    if collapsed:
        print(f"      ({collapsed} size/bundle variants collapsed onto {len(winners)} product offers)")
    applied = 0
    for p in winners:
        row = build_retailer_offer_row(
            product_key=p["matched_product_key"],
            merchant_id=sk.MERCHANT_ID, merchant_name=sk.MERCHANT_NAME,
            retailer_url=p["url"], market=sk.MARKET, currency=p.get("currency") or "USD",
            price=float(p["price"]), availability=p["record"]["offers"][0]["availability"],
        )
        await attach_retailer_offer(row)
        applied += 1
    return applied, eligible_missing + collapsed


def _like_escape(value: str) -> str:
    """Escape LIKE wildcards in a crawled brand string (backslash is Postgres's
    default LIKE escape). Injection-safe already (bound params); this prevents a
    stray %/_ in a scraped brand from turning the pattern into a table scan."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _load_our_rows(brands: List[str]) -> List[Dict[str, Any]]:
    """Canonical rows for the crawled brands (brand-token ILIKE, drift-tolerant)."""
    if not brands:
        return []
    clauses = " OR ".join(f"lower(brand) LIKE :b{i}" for i in range(len(brands)))
    params = {f"b{i}": f"%{_like_escape(b.lower())}%" for i, b in enumerate(brands)}
    rows = await database.fetch_all(
        f"SELECT product_key, content_key, brand, title, pdp_scope "
        f"FROM catalog_products WHERE title IS NOT NULL AND ({clauses})",
        params,
    )
    return [dict(r) for r in rows]


async def _drive(args: argparse.Namespace) -> int:
    # 1) enumerate this brand's PDP URLs from the sitemaps
    print(f"[1/5] enumerating StyleKorean sitemaps for brand '{args.brand}' ...", flush=True)
    index_xml = sk.fetch_text(sk.SITEMAP_INDEX)
    product_sitemaps = sk.product_sitemap_urls(index_xml)
    sitemap_texts = [sk.fetch_text(u) for u in product_sitemaps]
    urls = sk.brand_product_urls(args.brand, sitemap_texts)
    if args.limit:
        urls = urls[: args.limit]
    print(f"      {len(urls)} product URLs across {len(product_sitemaps)} sitemaps")
    if not urls:
        print("no product URLs — check the brand slug against sitemap-brands.xml")
        return 1

    # 2) crawl + extract (deterministic JSON-LD)
    print(f"[2/5] crawling {len(urls)} PDPs (concurrency={args.concurrency}) ...", flush=True)
    crawled = crawl_products(
        urls, concurrency=args.concurrency,
        on_progress=lambda i, n: (i % 25 == 0) and print(f"      {i}/{n}", flush=True),
    )
    records = crawled["records"]
    print(f"      extracted {len(records)} ({len(crawled['failures'])} failed/no-product)")

    # 3) match against our canonical catalog
    print("[3/5] matching to canonical catalog ...", flush=True)
    await database.connect()
    try:
        our_rows = await _load_our_rows(_brand_tokens(records))
        index = build_match_index(our_rows)
        plan: List[Dict[str, Any]] = []
        for r in records:
            rec = sk.to_validated_record(r)
            brand, title = rec["pdp"]["brand"], rec["pdp"]["product_name"]
            m = match_record(index, brand, title)
            off = rec["offers"][0]
            plan.append({
                "url": r["url"],
                "brand": brand,
                "title": title,
                "price": off["list_price"],
                "currency": off["currency"],
                "decision": "attach" if m else "mint",
                "match_key": retailer_match_key(brand, title),
                "matched_product_key": (m or {}).get("product_key"),
                "matched_content_key": (m or {}).get("content_key"),
                "record": rec,
            })
        attach = [p for p in plan if p["decision"] == "attach"]
        mint = [p for p in plan if p["decision"] == "mint"]
        print(f"      ATTACH {len(attach)}  |  MINT(new) {len(mint)}  |  our {args.brand} rows {len(our_rows)}")

        if args.out:
            with open(args.out, "w") as f:
                for p in plan:
                    f.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
            print(f"      wrote plan -> {args.out}")

        # 4) ATTACH lane — attach StyleKorean offers to already-owned canonicals
        print("[4/5] " + ("applying ATTACH offers ..." if args.apply_offers else "dry-run — no ATTACH writes (use --apply-offers)"), flush=True)
        if args.apply_offers:
            a, s = await _attach_offer_items(attach)
            print(f"      attached {a} offers ({s} skipped: no price)")

        # 5) MINT lane — brand-official-first. Ingest the brand's own storefront as
        #    the authentic canonical, so MINT SKUs attach to brand-direct content
        #    rather than being minted from StyleKorean's retailer copy (ADR-001).
        if not args.brand_official_domain:
            print("[5/5] MINT lane skipped (pass --brand-official-domain to enable)")
            for p in mint[:15]:
                print(f"      MINT  {p['title'][:56]:56} {p['currency']} {p['price']}")
            return 0

        print(f"[5/5] MINT via brand-official {args.brand_official_domain} ...", flush=True)
        brand_name = bo.dominant_brand(mint) or bo.dominant_brand(attach)
        official = await bo.fetch_official_records(
            domain=args.brand_official_domain, brand=brand_name, category_path=args.category_path,
        )
        resolved, residue = bo.predict_official_matches(mint, official)
        # Only NET-NEW brand-official products are safe to ingest — re-ingesting one
        # we already have would derive a new product_key from the storefront's drifted
        # title and duplicate the canonical (ingestion.derive_product_key has no strip).
        # suspect_drift (title-token containment vs an existing row, e.g. official
        # "Madagascar Centella Ampoule" vs our "Centella Ampoule") is propose-only:
        # routed to the residue/Gemini lane, never auto-minted.
        net_new, already_have, suspect_drift = bo.filter_net_new(official, our_rows)
        print(f"      brand-official products: {len(official)}  (already have {len(already_have)}, NET-NEW {len(net_new)}, suspect-drift {len(suspect_drift)})")
        print(f"      of {len(mint)} MINT SKUs → {len(resolved)} resolve to a brand-official product, {len(residue)} residue")
        for s in suspect_drift:
            residue.append({
                "brand": (s.get("pdp") or {}).get("brand"),
                "title": (s.get("pdp") or {}).get("product_name"),
                "record": s,
            })

        if args.residue_candidates:
            _write_residue_candidates(args.residue_candidates, residue,
                                      domain=args.brand_official_domain, category_path=args.category_path)
            print(f"      wrote {len(residue)} residue candidates -> {args.residue_candidates} (feed to run_catalog_enrichment.py)")

        if not args.apply_mint:
            print("      dry-run — no brand-official ingest (use --apply-mint)")
            for r in residue[:15]:
                print(f"      RESIDUE (no brand-official match)  {r['title'][:52]}")
            return 0

        summary = await bo.ingest_brand_official(
            official_records=net_new, brand=brand_name, domain=args.brand_official_domain,
            apply=True, db=database,
        )
        print(f"      ingested {len(net_new)} NET-NEW brand-official canonicals: {summary['applied']}")
        print(f"      ({len(already_have)} already-have skipped to avoid duplicate canonicals)")
        # re-match the MINT SKUs against the refreshed catalog and attach SK offers
        our_rows2 = await _load_our_rows(_brand_tokens(records))
        index2 = build_match_index(our_rows2)
        newly = []
        for p in mint:
            m2 = match_record(index2, p["brand"], p["title"])
            if m2:
                p = {**p, "matched_product_key": m2["product_key"], "matched_content_key": m2["content_key"]}
                newly.append(p)
        print(f"      MINT SKUs now matching a canonical after ingest: {len(newly)}")
        if args.apply_offers and newly:
            a, s = await _attach_offer_items(newly)
            print(f"      attached {a} StyleKorean offers to brand-official canonicals ({s} skipped: no price)")
        print(f"      residue still unmatched (flag for review): {len(mint) - len(newly)}")
    finally:
        await database.disconnect()
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--brand", required=True, help="StyleKorean brand slug, e.g. cosrx (see sitemap-brands.xml)")
    p.add_argument("--limit", type=int, default=0, help="cap number of PDPs (0 = all)")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--out", default=None, help="write the attach/mint plan as JSONL")
    p.add_argument("--apply-offers", action="store_true", help="write the ATTACH lane (retailer offers on matched canonicals).")
    p.add_argument("--brand-official-domain", default=None, help="brand's official Shopify domain (e.g. cosrx.com) — enables the MINT lane.")
    p.add_argument("--category-path", default="beauty/skincare", help="taxonomy category_path for brand-official ingest.")
    p.add_argument("--apply-mint", action="store_true", help="ingest the brand-official catalog as canonicals (MINT). Requires --brand-official-domain.")
    p.add_argument("--residue-candidates", default=None, help="write residue SKUs as catalog_enrichment_agent candidates JSONL (Gemini PDP-resolution handoff).")
    args = p.parse_args()
    if args.apply_mint and not args.brand_official_domain:
        p.error("--apply-mint requires --brand-official-domain")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_drive(_parse_args())))
