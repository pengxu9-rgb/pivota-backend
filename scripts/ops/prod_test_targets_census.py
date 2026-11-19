#!/usr/bin/env python3
"""READ-ONLY census: candidate ids for the prod test-transaction job's private targets file.

docs/runbooks/prod_test_transactions.md describes the file; scripts/ops/prod_test_transactions.py (parse_targets)
checks it. This census finds AT MOST 3 candidates for each of the three target fields, using the predicates the
SERVING code uses, so a candidate behaves the way the job's cases expect. It writes nothing and proposes nothing; the
owner and the controller pick the ids.

SECTIONS (one `PTT_TARGETS {...}` line each)
  test_merchants    KNOWN_TEST_MERCHANT_IDS (services/test_merchant_policy.py) plus merchant_onboarding.is_test;
                    count and ids.
  offers            offers_product_id: unexpired products_cache rows of a test merchant (offers.resolve's internal
                    arm: routes/agent_shop_gateway.py, products_cache WHERE expires_at > NOW() by platform_product_id
                    alias). Per row: the row's currency (variant, else product, else currency_code, else USD), and
                    the SGD sources OR-SG needs: active, unquarantined external_product_seeds attached to that product
                    with price_currency SGD, and other unexpired products_cache rows with the same platform_product_id
                    priced in SGD. Also the count of unexpired test-merchant rows. A USD row with no SGD source means
                    OR-SG cannot be served from it (one row = one currency).
  kernel            kernel_test_merchant: a test merchant whose first merchant_stores row (status active/connected,
                    is_primary first) is a Shopify store with a domain, plus one products_cache product and its first
                    variant. Reported: whether a store key is present and whether it names a storefront token
                    (booleans only, the key itself is never selected), the variant id (bare digits are what the kernel
                    quote wraps as gid://shopify/ProductVariant/N), the currency and availability as cached. The live
                    Shopify answer cannot be proven offline.
  storefront        storefront_seller: merch_obs_ catalog rows from the external seed mirror, live, unsuppressed,
                    serving-eligible (index_pipeline_state), joined to an active, unquarantined US/USD seed with an
                    https destination; its lower-case host (www. stripped), a variant id the shadow catalog lookup can
                    read as a Shopify variant (6+ digits or a variant gid), whether every seed variant is USD, and the
                    newest US merchant_purchasability verdict for the host.

OUTPUT. Every line is PTT_TARGETS followed by one JSON object. The ids printed are merchant, product and variant ids
and store hosts, for the PRIVATE log only; nothing secret is selected.

SAFETY. One READ ONLY, REPEATABLE READ transaction (asserted with SHOW transaction_read_only), SET LOCAL
statement_timeout 90 s, lock_timeout 5 s, idle_in_transaction_session_timeout 300 s, ROLLED BACK in a finally. Each
statement runs once; a timeout is a result, not something to retry. Fetches nothing from any merchant. Carries no
at-sign, so it can be passed inline:

    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/prod_test_targets_census.py)"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List

MARKER = "PTT_TARGETS "
LIMIT = 3
STATEMENT_TIMEOUT = "90s"
LOCK_TIMEOUT = "5s"
IDLE_TIMEOUT = "300s"
PURCHASABLE_VERDICTS = ("ELIGIBLE", "NO_CARD_PAYMENT", "PRICE_DRIFT")

TEST_MERCHANTS_SQL = "SELECT merchant_id FROM merchant_onboarding WHERE is_test = TRUE"

# The products_cache row's currency, as offers.resolve reads it (variant, product, currency_code, default USD).
ROW_CURRENCY = ("upper(trim(coalesce(pc.product_data->'variants'->0->>'currency', pc.product_data->>'currency', "
                "pc.product_data->>'currency_code', 'USD')))")

OFFERS_SQL = f"""
SELECT pc.merchant_id, pc.platform, pc.platform_product_id, {ROW_CURRENCY} AS currency,
       pc.expires_at,
       (SELECT count(*) FROM external_product_seeds
         WHERE external_product_seeds.status = 'active'
           AND external_product_seeds.attached_product_key
               = 'prod::' || pc.merchant_id || '::' || pc.platform || '::' || pc.platform_product_id
           AND upper(trim(coalesce(external_product_seeds.price_currency, ''))) = 'SGD'
           {{quarantine}}) AS sgd_seed_sources,
       (SELECT count(*) FROM products_cache other
         WHERE other.id <> pc.id AND (other.expires_at IS NULL OR other.expires_at > NOW())
           AND other.platform_product_id = pc.platform_product_id
           AND upper(trim(coalesce(other.product_data->'variants'->0->>'currency', other.product_data->>'currency',
                                   other.product_data->>'currency_code', 'USD'))) = 'SGD') AS sgd_cache_sources
FROM products_cache pc
WHERE pc.merchant_id = ANY($1::text[]) AND (pc.expires_at IS NULL OR pc.expires_at > NOW())
  AND pc.platform_product_id !~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
ORDER BY (CASE WHEN {ROW_CURRENCY} = 'USD' THEN 0 ELSE 1 END), 6 DESC, 7 DESC, pc.expires_at DESC NULLS FIRST
LIMIT {LIMIT}
"""

UNEXPIRED_TEST_ROWS_SQL = """
SELECT count(*) AS rows, count(DISTINCT merchant_id) AS merchants FROM products_cache
WHERE merchant_id = ANY($1::text[]) AND (expires_at IS NULL OR expires_at > NOW())
"""

KERNEL_SQL = f"""
WITH store AS (
    SELECT DISTINCT ON (ms.merchant_id) ms.merchant_id, lower(ms.platform) AS platform, ms.domain,
           (ms.api_key IS NOT NULL AND length(ms.api_key) > 0) AS store_key_present,
           (coalesce(ms.api_key, '') ILIKE '%storefront%') AS store_key_names_storefront
    FROM merchant_stores ms
    WHERE ms.merchant_id = ANY($1::text[]) AND ms.status IN ('active', 'connected')
    ORDER BY ms.merchant_id, ms.is_primary DESC, ms.connected_at DESC
)
SELECT s.merchant_id, s.platform, s.domain, s.store_key_present, s.store_key_names_storefront,
       pc.platform_product_id AS product_id,
       coalesce(pc.product_data->'variants'->0->>'variant_id', pc.product_data->'variants'->0->>'id') AS variant_id,
       {ROW_CURRENCY} AS currency,
       pc.product_data->'variants'->0->>'available' AS variant_available,
       pc.product_data->'variants'->0->>'inventory_quantity' AS variant_inventory,
       (pc.expires_at IS NULL OR pc.expires_at > NOW()) AS cache_unexpired
FROM store s
JOIN products_cache pc ON pc.merchant_id = s.merchant_id
WHERE s.platform = 'shopify' AND coalesce(s.domain, '') <> ''
  AND coalesce(pc.product_data->'variants'->0->>'variant_id', pc.product_data->'variants'->0->>'id', '') ~ '^[0-9]{{6,}}$'
ORDER BY s.store_key_names_storefront DESC, (CASE WHEN {ROW_CURRENCY} = 'USD' THEN 0 ELSE 1 END), pc.cached_at DESC
LIMIT {LIMIT}
"""

SEED_HOST = ("lower(regexp_replace(split_part(split_part(coalesce(external_product_seeds.destination_url, "
             "external_product_seeds.canonical_url, ''), '://', 2), '/', 1), '^www\\.', ''))")
SEED_VARIANT = ("coalesce(external_product_seeds.seed_data->'variants'->0->>'source_variant_id', "
                "external_product_seeds.seed_data->'variants'->0->>'variant_gid', "
                "external_product_seeds.seed_data->'variants'->0->>'variant_id', "
                "external_product_seeds.seed_data->'variants'->0->>'id')")

STOREFRONT_SQL = f"""
SELECT cp.merchant_id, external_product_seeds.external_product_id AS product_id, {SEED_VARIANT} AS variant_id,
       {SEED_HOST} AS host,
       jsonb_array_length(coalesce(external_product_seeds.seed_data->'variants', '[]'::jsonb)) AS variants,
       NOT EXISTS (SELECT 1 FROM jsonb_array_elements(coalesce(external_product_seeds.seed_data->'variants', '[]'::jsonb)) v
                   WHERE upper(trim(coalesce(v->>'currency', 'USD'))) <> 'USD') AS all_variants_usd,
       external_product_seeds.availability,
       (SELECT mp.verdict FROM merchant_purchasability mp
         WHERE mp.merchant_domain = {SEED_HOST} AND mp.market_country = 'US'
         ORDER BY mp.checked_at DESC NULLS LAST LIMIT 1) AS purchasability_verdict,
       (SELECT mp.checked_at FROM merchant_purchasability mp
         WHERE mp.merchant_domain = {SEED_HOST} AND mp.market_country = 'US'
         ORDER BY mp.checked_at DESC NULLS LAST LIMIT 1) AS purchasability_checked_at
FROM catalog_products cp
JOIN index_pipeline_state ips ON ips.content_key = cp.content_key AND ips.serving_eligible
JOIN external_product_seeds ON external_product_seeds.external_product_id = cp.source_product_id
WHERE cp.merchant_id LIKE 'merch\\_obs\\_%' AND cp.source_system = 'external_product_seeds_mirror_v1'
  AND cp.sync_status = 'live' AND cp.suppressed_at IS NULL AND cp.suppression_reason IS NULL
  AND external_product_seeds.status = 'active' AND upper(trim(external_product_seeds.market)) = 'US'
  AND upper(trim(coalesce(external_product_seeds.price_currency, ''))) = 'USD'
  AND coalesce(external_product_seeds.destination_url, '') LIKE 'https://%'
  AND {SEED_VARIANT} ~ '^[0-9]{{6,}}$|gid://shopify/ProductVariant/[0-9]+'
  {{quarantine}}
ORDER BY (SELECT CASE WHEN mp.verdict IN {PURCHASABLE_VERDICTS!r} THEN 0 ELSE 1 END FROM merchant_purchasability mp
           WHERE mp.merchant_domain = {SEED_HOST} AND mp.market_country = 'US'
           ORDER BY mp.checked_at DESC NULLS LAST LIMIT 1) NULLS LAST,
         6 DESC, cp.merchant_id
LIMIT {LIMIT}
"""


def test_merchant_ids(known: List[str], onboarding: List[str]) -> List[str]:
    return sorted({str(m) for m in list(known) + list(onboarding) if m})


def sql(template: str, quarantine: str) -> str:
    return template.replace("{quarantine}", quarantine)


async def census(conn: Any, *, known: List[str], quarantine: str) -> List[Dict[str, Any]]:
    tr = conn.transaction(readonly=True, isolation="repeatable_read")
    await tr.start()
    try:
        assert (await conn.fetchval("SHOW transaction_read_only")) == "on", "the census must run READ ONLY"
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        await conn.execute(f"SET LOCAL idle_in_transaction_session_timeout = '{IDLE_TIMEOUT}'")
        ids = test_merchant_ids(known, [r["merchant_id"] for r in await conn.fetch(TEST_MERCHANTS_SQL)])
        lines: List[Dict[str, Any]] = [{"kind": "test_merchants", "count": len(ids), "ids": ids}]
        unexpired = await conn.fetchrow(UNEXPIRED_TEST_ROWS_SQL, ids)
        lines.append({"kind": "offers_scope", "unexpired_test_rows": unexpired["rows"],
                      "unexpired_test_merchants": unexpired["merchants"]})
        for row in await conn.fetch(sql(OFFERS_SQL, quarantine), ids):
            lines.append({"kind": "offers", **dict(row)})
        for row in await conn.fetch(KERNEL_SQL, ids):
            lines.append({"kind": "kernel", **dict(row)})
        for row in await conn.fetch(sql(STOREFRONT_SQL, quarantine)):
            lines.append({"kind": "storefront", **dict(row)})
        return lines
    finally:
        await tr.rollback()


def render(lines: List[Dict[str, Any]]) -> List[str]:
    return [MARKER + json.dumps(line, default=str, sort_keys=True) for line in lines]


async def _main() -> int:
    import asyncpg

    from services.external_seed_search import build_seed_quarantine_anti_join
    from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url.startswith("postgres"):
        print(MARKER + json.dumps({"kind": "error", "error": "DATABASE_URL must be Postgres"}), flush=True)
        return 2
    conn = await asyncpg.connect(url)
    try:
        lines = await census(conn, known=sorted(KNOWN_TEST_MERCHANT_IDS), quarantine=build_seed_quarantine_anti_join())
    finally:
        await conn.close()
    for text in render(lines):
        print(text, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
