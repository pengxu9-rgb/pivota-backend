"""READ-ONLY prod dry run of the mirror variant re-price (be#2684), before the refresh job is re-imaged.

Plans EVERY variant row on every unsuppressed mirror product (the ones the lane resolves) with the lane's own planner,
`services.external_offer_dual_write.plan_mirror_variant_writes`, over the rows the lane itself reads
(`ATTACHED_LISTING_OFFERS_SQL`, the seed through `_SEED_OFFER_COLUMNS`). Writes nothing: one READ ONLY
transaction per chunk of products, with a statement timeout from DB_STATEMENT_TIMEOUT_SECONDS.

It needs the MERGED image: the planner is imported, never restated, so this plans exactly the code
that will run. On an image without it the gate stays closed.

What it cannot know at rest: whether tonight's page read will name its currency (`currency_read`).
It plans as if it does (the refresh's normal case) and says so on the summary line. The seed's
`variant_refresh.status` is the last refresh's, so the plan is the one that refresh would make.

No at-sign anywhere in this file: run_oneoff_job.sh picks its --args delimiter from a fixed set.

Launch (Peng; pg CPU gate first):
  ENV_VARS=PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600 \\
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/plan_mirror_variant_reprice.py)"

Output:
  * `GATE_OPEN` once every precondition holds (production env, statement timeout set, a read-only
    transaction, the tables present, the planner importable), else `GATE_CLOSED <reason>` and exit 3
    before a single plan query;
  * then only `MVR_PLAN <kind> {json}` lines: `progress`, `summary`, `skips`, `statuses`,
    `price_moves`, `sample` (before/after of planned writes) and `refused_sample`;
  * last, `MVR_PLAN DONE lines=N`, N = the MVR_PLAN lines before it. A run that stops early has none.
"""

import asyncio
import json
import os
import re
import sys

# `python scripts/ops/<this>.py` puts scripts/ops/ on sys.path, and `python -c` has no __file__.
# /app is the image's repo root.
try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)

PREFIX = "MVR_PLAN"
CHUNK = 200
SAMPLE = 40
_lines = {"n": 0}

MIRROR_PRODUCTS_SQL = """
SELECT p.product_key, p.merchant_id, p.source_ref AS seed_id, to_jsonb(p) ->> 'sync_status' AS sync_status
FROM catalog_products p
WHERE p.source_system = 'external_product_seeds_mirror_v1'
  AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
  AND EXISTS (SELECT 1 FROM catalog_offers o
              WHERE o.product_key = p.product_key AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL
                AND o.sku_key <> p.product_key || '::canonical')
ORDER BY p.product_key
"""


def _closed(reason):
    print("GATE_CLOSED " + reason, flush=True)
    sys.exit(3)


def emit(kind, payload):
    print(f"{PREFIX} {kind} " + json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True), flush=True)
    _lines["n"] += 1


def _numbered(sql):
    """`:name` binds (the app's style) to asyncpg's `$n`, leaving `::casts` alone."""
    names = []

    def sub(m):
        if m.group(1) not in names:
            names.append(m.group(1))
        return "$" + str(names.index(m.group(1)) + 1)

    return re.sub(r"(?<!:):([a-z_]+)", sub, sql), names


def _bump(counter, key, n=1):
    counter[key] = counter.get(key, 0) + n


async def main():
    import asyncpg

    if os.getenv("PIVOTA_ENV") != "production":
        _closed("PIVOTA_ENV is not production")
    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        _closed("DATABASE_URL missing")
    timeout_s = float(os.getenv("DB_STATEMENT_TIMEOUT_SECONDS") or 0)
    if timeout_s <= 0:
        _closed("DB_STATEMENT_TIMEOUT_SECONDS unset: refusing to run without a statement timeout")
    try:
        from services import external_offer_dual_write as mod
        from services.seed_offer_currency import SeedOfferCurrencyRefused
    except Exception as exc:  # noqa: BLE001
        _closed("cannot import the lane: " + repr(exc)[:200])
    if not hasattr(mod, "plan_mirror_variant_writes"):
        _closed("this image has no plan_mirror_variant_writes: run it on the merged image")
    seed_sql, seed_names = _numbered(f"SELECT {mod._SEED_OFFER_COLUMNS} FROM external_product_seeds WHERE id = :seed_id")
    offers_sql, offer_names = _numbered(mod.ATTACHED_LISTING_OFFERS_SQL)
    if seed_names != ["seed_id"] or offer_names != ["product_key"]:
        _closed("the lane's statements changed their binds: update this script")

    conn = await asyncpg.connect(url, command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT_SECONDS") or 600))

    async def read_only():
        tr = conn.transaction(readonly=True)
        await tr.start()
        await conn.execute("SET LOCAL statement_timeout = " + str(int(timeout_s * 1000)))
        if await conn.fetchval("SHOW transaction_read_only") != "on":
            await tr.rollback()
            _closed("transaction is not read-only")
        return tr

    try:
        tr = await read_only()
        present = await conn.fetchrow(
            """
            SELECT to_regclass('catalog_products') IS NOT NULL AS products,
                   to_regclass('catalog_offers') IS NOT NULL AS offers,
                   to_regclass('catalog_skus') IS NOT NULL AS skus,
                   to_regclass('external_product_seeds') IS NOT NULL AS seeds
            """
        )
        for need in ("products", "offers", "skus", "seeds"):
            if not present[need]:
                await tr.rollback()
                _closed("table missing: " + need)
        products = [dict(r) for r in await conn.fetch(MIRROR_PRODUCTS_SQL)]
        await tr.rollback()
        print("GATE_OPEN", flush=True)

        # Every unsuppressed mirror the lane would resolve (`resolve_mirror_product` does not read
        # sync_status), counted by sync_status so the live share is visible.
        totals = {"products": len(products), "variant_rows": 0, "planned_writes": 0, "price_moves": 0,
                  "price_moves_over_10pct": 0, "ours_lower_now": 0, "same_price_restamp": 0}
        sync_states = {}
        for product in products:
            _bump(sync_states, str(product["sync_status"]))
        statuses, skips, seed_states = {}, {}, {}
        sample, refused_sample = [], []
        for start in range(0, len(products), CHUNK):
            tr = await read_only()
            try:
                for product in products[start:start + CHUNK]:
                    pk, merchant = product["product_key"], product["merchant_id"]
                    seed_row = await conn.fetchrow(seed_sql, str(product["seed_id"] or ""))
                    rows = [dict(r) for r in await conn.fetch(offers_sql, pk)]
                    n_variant = sum(1 for r in rows if r["sku_key"] != mod.derive_mirror_sku_key(pk)
                                    and r["offer_id"] != mod.derive_mirror_offer_id(pk))
                    totals["variant_rows"] += n_variant
                    if not seed_row:
                        _bump(statuses, "seed_missing")
                        _bump(skips, "seed_missing", n_variant)
                        continue
                    seed = dict(seed_row)
                    _bump(seed_states, str(seed.get("variant_refresh_status")))
                    try:
                        plan = mod.plan_mirror_variant_writes(seed, pk, merchant, rows, currency_read=True)
                    except SeedOfferCurrencyRefused:
                        # The mirror upsert refuses this seed first, so the lane never runs for it.
                        _bump(statuses, "seed_refused_currency")
                        _bump(skips, "seed_refused_currency", n_variant)
                        continue
                    _bump(statuses, plan["status"])
                    for reason, n in (plan.get("skips") or {}).items():
                        _bump(skips, reason, n)
                    by_id = {r["offer_id"]: r for r in plan.get("rows") or []}
                    for write in plan.get("writes") or []:
                        totals["planned_writes"] += 1
                        before = mod._current_price(by_id[write["offer_id"]])
                        after = write["price"]
                        if before is None or abs(after - before) > 0.005:
                            totals["price_moves"] += 1
                            if before and abs(after - before) > 0.10 * before:
                                totals["price_moves_over_10pct"] += 1
                            if before is not None and before < after:
                                totals["ours_lower_now"] += 1
                            if len(sample) < SAMPLE:
                                row = by_id[write["offer_id"]]
                                sample.append({"offer_id": write["offer_id"], "product_key": pk,
                                               "variant_id": row.get("source_variant_id"),
                                               "currency": write["currency"], "before": before, "after": after})
                        else:
                            totals["same_price_restamp"] += 1
                    for refused in plan.get("refused") or []:
                        if len(refused_sample) < 10:
                            refused_sample.append({"product_key": pk, **refused})
            finally:
                await tr.rollback()
            emit("progress", {"products_done": min(start + CHUNK, len(products)), "of": len(products)})
        emit("summary", {**totals, "assumes_currency_read": True, "products_by_sync_status": sync_states})
        emit("statuses", statuses)
        emit("skips", skips)
        emit("variant_refresh_status", seed_states)
        emit("sample", sample)
        emit("refused_sample", refused_sample)
    finally:
        await conn.close()
    print(f"{PREFIX} DONE lines={_lines['n']}", flush=True)


asyncio.run(main())
