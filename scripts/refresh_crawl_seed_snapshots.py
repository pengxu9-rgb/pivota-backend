"""Re-extract crawl-onboarded seeds from their live PDPs and re-stamp
snapshot.extracted_at — the honest way to clear the 7d stale_snapshot gate.

Why: external_brand_crawl seeds are runtime-gated by stale_snapshot
(services/external_referral_readiness.py, EXTERNAL_REFERRAL_STALE_DAYS=7) and
nothing refreshes them — run_external_referral_refresh_batch has no callers.
Re-onboarding an old cohort file would stamp stale data as fresh; this script
instead RE-FETCHES each product's live Shopify JSON (<destination_url>.json),
updates the sellable facts (title, price, availability, REAL variants — which
also replace the synthesized crawl_default_variant_v1 authored by
scripts/backfill_crawl_seed_variants.py), and only then stamps
snapshot.extracted_at with the actual fetch time. Products that 404 or fail to
parse are left untouched (never stamped fresh) and reported.

Scope: seed_data.snapshot is MERGED (title/variants/extracted_at keys only) —
INCI/enrichment keys (pdp_ingredients_raw, inci_list, derived.recall...) are
preserved, unlike a full re-onboard whose _upsert_seed replaces seed_data
wholesale. updated_at IS bumped (a real re-extraction happened).

Safety: dry-run by default (--apply to write); --host / --epid-prefix pilot
slices; optimistic updated_at guard; batched UNNEST writes (row-by-row UPDATEs
through the Railway public proxy run at ~5/min — do not do that).

Usage (from repo root; local runs need DATABASE_URL=$DATABASE_PUBLIC_URL):
  python -m scripts.refresh_crawl_seed_snapshots --host acropass.us          # dry-run pilot
  python -m scripts.refresh_crawl_seed_snapshots --host acropass.us --apply
  python -m scripts.refresh_crawl_seed_snapshots --apply                     # all stale
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncpg
import httpx

TOOL = "external_brand_crawl"
USER_AGENT = "PivotaCrawler/1.0 (+https://www.pivota.cc; catalog freshness refresh)"
FETCH_TIMEOUT_S = 15.0
PER_HOST_DELAY_S = 0.35
HOST_CONCURRENCY = 6
WRITE_BATCH_SIZE = 200


def _dsn() -> str:
    import os

    return os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL") or ""


def _ensure_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _product_json_url(destination_url: str) -> Optional[str]:
    raw = str(destination_url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not raw or "/products/" not in raw:
        return None
    return raw + ".json"


def _host(destination_url: str) -> str:
    try:
        return (urlparse(str(destination_url or "")).hostname or "").lower()
    except Exception:
        return ""


def build_refreshed_snapshot_fields(
    product: Dict[str, Any], *, currency: str, fallback_image: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Sellable facts from a live Shopify product JSON ({"product": {...}})."""
    title = str(product.get("title") or "").strip()
    raw_variants = product.get("variants")
    if not title or not isinstance(raw_variants, list) or not raw_variants:
        return None
    images = product.get("images")
    product_image = None
    if isinstance(images, list) and images and isinstance(images[0], dict):
        product_image = str(images[0].get("src") or "").strip() or None
    product_image = product_image or fallback_image
    variants: List[Dict[str, Any]] = []
    any_available = False
    first_price: Optional[float] = None
    for raw in raw_variants:
        if not isinstance(raw, dict):
            continue
        try:
            price = float(raw.get("price")) if raw.get("price") is not None else None
        except Exception:
            price = None
        # /products/<handle>.json omits `available` on some shops; treat
        # missing as available (parity with the row default) but honor an
        # explicit false.
        available = raw.get("available")
        is_available = True if available is None else bool(available)
        any_available = any_available or is_available
        if first_price is None and price is not None:
            first_price = price
        vid = str(raw.get("id") or "").strip() or f"variant-{len(variants) + 1}"
        variants.append(
            {
                "id": vid,
                "variant_id": vid,
                "sku": str(raw.get("sku") or "").strip() or vid,
                "title": str(raw.get("title") or "").strip() or title,
                "price_amount": price,
                "price": price,
                "currency": currency,
                "availability": "in_stock" if is_available else "out_of_stock",
                "image_url": product_image,
                "source": "crawl_refresh_v1",
            }
        )
    if not variants:
        return None
    return {
        "title": title,
        "variants": variants,
        "image_url": product_image,
        "price_amount": first_price,
        "availability": "in_stock" if any_available else "out_of_stock",
    }


async def _fetch_product(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(url, timeout=FETCH_TIMEOUT_S, follow_redirects=True)
        if resp.status_code != 200:
            return None
        body = resp.json()
        product = body.get("product") if isinstance(body, dict) else None
        return product if isinstance(product, dict) else None
    except Exception:
        return None


async def run(
    *, max_age_hours: int, host_filter: str, epid_prefix: str, limit: int, apply: bool
) -> None:
    conn = await asyncpg.connect(_dsn(), ssl=False)
    http = httpx.AsyncClient(headers={"User-Agent": USER_AGENT})
    try:
        rows = await conn.fetch(
            """
            SELECT id, external_product_id, destination_url, price_currency,
                   image_url, updated_at, seed_data
            FROM external_product_seeds
            WHERE tool = $1 AND status = 'active'
              AND jsonb_typeof(seed_data) = 'object'
              AND COALESCE(
                    (seed_data->'snapshot'->>'extracted_at')::timestamptz,
                    updated_at, created_at
                  ) < now() - make_interval(hours => $2)
              AND ($3 = '' OR split_part(split_part(destination_url,'//',2),'/',1) = $3)
              AND ($4 = '' OR external_product_id LIKE $5)
            ORDER BY destination_url
            LIMIT $6
            """,
            TOOL,
            max_age_hours,
            host_filter,
            epid_prefix,
            f"{epid_prefix}%",
            limit,
        )
        by_host: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            r = dict(row)
            by_host.setdefault(_host(r.get("destination_url")), []).append(r)

        refreshed_count = 0
        fetch_failed = 0
        no_json_url = 0
        parse_failed = 0
        written = 0
        conflicts = 0
        samples: List[Dict[str, Any]] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        semaphore = asyncio.Semaphore(HOST_CONCURRENCY)
        # Fetches run concurrently (httpx), but the single asyncpg connection
        # forbids concurrent operations — serialize all writes so two hosts
        # finishing together can't collide on it.
        write_lock = asyncio.Lock()

        async def _write_batch(chunk: List[Dict[str, Any]]) -> None:
            # Per-host incremental write so a mid-run interrupt (session
            # teardown, network drop) preserves completed hosts — a re-run's
            # WHERE clause already excludes freshly-stamped seeds, so it
            # resumes from where it stopped. Batched UNNEST (row-by-row through
            # the Railway public proxy runs at ~5/min).
            nonlocal written, conflicts
            for start in range(0, len(chunk), WRITE_BATCH_SIZE):
                batch = chunk[start : start + WRITE_BATCH_SIZE]
                async with write_lock:
                    result = await conn.fetch(
                    """
                    UPDATE external_product_seeds AS eps
                    SET seed_data = CAST(u.seed_data AS jsonb),
                        title = u.title,
                        price_amount = u.price_amount,
                        availability = u.availability,
                        image_url = COALESCE(u.image_url, eps.image_url),
                        updated_at = NOW()
                    FROM unnest(
                        $1::text[], $2::text[], $3::text[], $4::float8[],
                        $5::text[], $6::text[], $7::timestamptz[]
                    ) AS u(id, seed_data, title, price_amount, availability, image_url, prev_updated_at)
                    WHERE eps.id = u.id
                      AND eps.updated_at IS NOT DISTINCT FROM u.prev_updated_at
                    RETURNING eps.id
                    """,
                    [c["id"] for c in batch],
                    [json.dumps(c["seed_data"], ensure_ascii=False, default=str) for c in batch],
                    [c["title"] for c in batch],
                    [c["price_amount"] for c in batch],
                    [c["availability"] for c in batch],
                    [c["image_url"] for c in batch],
                    [c["prev_updated_at"] for c in batch],
                )
                written += len(result)
                conflicts += len(batch) - len(result)

        async def _refresh_host(host: str, seeds: List[Dict[str, Any]]) -> None:
            nonlocal fetch_failed, no_json_url, parse_failed, refreshed_count
            host_refreshed: List[Dict[str, Any]] = []
            async with semaphore:
                for r in seeds:
                    url = _product_json_url(r.get("destination_url"))
                    if not url:
                        no_json_url += 1
                        continue
                    product = await _fetch_product(http, url)
                    if product is None:
                        fetch_failed += 1
                        await asyncio.sleep(PER_HOST_DELAY_S)
                        continue
                    fields = build_refreshed_snapshot_fields(
                        product,
                        currency=(str(r.get("price_currency") or "").strip() or "USD"),
                        fallback_image=r.get("image_url"),
                    )
                    if fields is None:
                        parse_failed += 1
                        await asyncio.sleep(PER_HOST_DELAY_S)
                        continue
                    seed_data = _ensure_obj(r.get("seed_data"))
                    snapshot = _ensure_obj(seed_data.get("snapshot"))
                    snapshot["title"] = fields["title"]
                    snapshot["variants"] = fields["variants"]
                    snapshot["extracted_at"] = now_iso
                    seed_data["snapshot"] = snapshot
                    host_refreshed.append(
                        {
                            "id": r["id"],
                            "prev_updated_at": r["updated_at"],
                            "seed_data": seed_data,
                            "title": fields["title"],
                            "price_amount": fields["price_amount"],
                            "availability": fields["availability"],
                            "image_url": fields["image_url"],
                        }
                    )
                    await asyncio.sleep(PER_HOST_DELAY_S)
            refreshed_count += len(host_refreshed)
            if len(samples) < 3 and host_refreshed:
                c = host_refreshed[0]
                samples.append(
                    {
                        "id": c["id"],
                        "title": c["title"],
                        "price_amount": c["price_amount"],
                        "availability": c["availability"],
                        "n_variants": len(c["seed_data"]["snapshot"]["variants"]),
                    }
                )
            if apply and host_refreshed:
                await _write_batch(host_refreshed)

        await asyncio.gather(*(_refresh_host(h, s) for h, s in by_host.items()))

        print(
            json.dumps(
                {
                    "mode": "apply" if apply else "dry_run",
                    "max_age_hours": max_age_hours,
                    "host": host_filter or None,
                    "candidates": len(rows),
                    "hosts": len(by_host),
                    "refreshed": refreshed_count,
                    "fetch_failed": fetch_failed,
                    "no_json_url": no_json_url,
                    "parse_failed": parse_failed,
                    "written": written,
                    "conflicts": conflicts,
                    "sample": samples,
                },
                ensure_ascii=False,
                indent=1,
                default=str,
            )
        )
    finally:
        await http.aclose()
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-hours", type=int, default=24)
    parser.add_argument("--host", default="", help="single registrable host pilot slice")
    parser.add_argument("--epid-prefix", default="")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        run(
            max_age_hours=args.max_age_hours,
            host_filter=args.host.strip().lower(),
            epid_prefix=args.epid_prefix,
            limit=args.limit,
            apply=args.apply,
        )
    )


if __name__ == "__main__":
    main()
