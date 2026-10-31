"""READ-ONLY prod census for the unbuildable agent_pdp_view reap (be#2447).

Counts the view rows `reap_unbuildable_agent_pdp_view_rows` would delete, by reason and lifecycle
stage, the ones still serving_eligible / index_eligible in index_pipeline_state (the IPS flips the
delete causes: has_image is read off the view), and the agent_decision_funnel_links whose
content_key each delete would null. Writes nothing: one READ ONLY transaction, statement timeout
from DB_STATEMENT_TIMEOUT_SECONDS.

It is written to run as an INLINE program on the CURRENT production image (main), which does not
carry this PR's assembler code, so the predicate is restated here from main's own
_SOURCE_QUARANTINE_ANTI_JOIN. Keep it identical to
services.agent_pdp_view_assembler._unbuildable_predicate (incl. the title_override arm).
No at-sign anywhere in this file: run_oneoff_job.sh picks its --args delimiter from a fixed set.

Launch (controller; pg CPU gate first):
  ENV_VARS=PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600,AGENT_PDP_VIEW_MARKET_PRICES=on \\
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/census_unbuildable_agent_pdp_view.py)"

Output: a `GATE_OPEN` line only once every precondition holds (production env, a read-only
transaction, the tables present), then one `CENSUS {json}` line, then `CENSUS_DONE`. Any failed
precondition prints `GATE_CLOSED <reason>` and exits 3 before a single census query.
"""

import asyncio
import json
import os
import sys

# `python scripts/ops/<this>.py` puts scripts/ops/ on sys.path, and `python -c` has no __file__.
# /app is the image's repo root.
try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)


def _closed(reason):
    print("GATE_CLOSED " + reason, flush=True)
    sys.exit(3)


async def main():
    import asyncpg

    from services.agent_pdp_view_assembler import _SOURCE_QUARANTINE_ANTI_JOIN as AJ

    if os.getenv("PIVOTA_ENV") != "production":
        _closed("PIVOTA_ENV is not production")
    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        _closed("DATABASE_URL missing")
    timeout_s = float(os.getenv("DB_STATEMENT_TIMEOUT_SECONDS") or 0)
    if timeout_s <= 0:
        _closed("DB_STATEMENT_TIMEOUT_SECONDS unset: refusing to run without a statement timeout")
    conn = await asyncpg.connect(url, command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT_SECONDS") or 600))
    try:
        tr = conn.transaction(readonly=True)
        await tr.start()
        try:
            await conn.execute("SET LOCAL statement_timeout = " + str(int(timeout_s * 1000)))
            if await conn.fetchval("SHOW transaction_read_only") != "on":
                _closed("transaction is not read-only")
            present = await conn.fetchrow(
                """
                SELECT to_regclass('agent_pdp_view') IS NOT NULL AS view,
                       to_regclass('catalog_products') IS NOT NULL AS products,
                       to_regclass('catalog_source_quarantine') IS NOT NULL AS quarantine,
                       to_regclass('index_pipeline_state') IS NOT NULL AS ips,
                       to_regclass('product_enrichment') IS NOT NULL AS enrichment,
                       to_regclass('agent_decision_funnel_links') IS NOT NULL AS links,
                       to_regclass('agent_pdp_view_unbuildable_reap_audit') IS NOT NULL AS audit
                """
            )
            for need in ("view", "products", "quarantine", "ips", "links"):
                if not present[need]:
                    _closed("table missing: " + need)
            print("GATE_OPEN", flush=True)

            titled = f"""EXISTS (SELECT 1 FROM catalog_products cp WHERE cp.content_key = av.content_key
                    AND cp.title IS NOT NULL AND trim(cp.title) <> '' {AJ})"""
            override = f"""EXISTS (SELECT 1 FROM catalog_products cp JOIN product_enrichment pe
                    ON pe.merchant_id = cp.merchant_id AND pe.platform = cp.platform
                   AND pe.platform_product_id = cp.source_product_id AND pe.geo_code = 'default'
                    WHERE cp.content_key = av.content_key
                      AND pe.title_override IS NOT NULL AND trim(pe.title_override) <> '' {AJ})"""
            if not present["enrichment"]:
                override = "FALSE"
            survivor = f"""EXISTS (SELECT 1 FROM catalog_products cp WHERE cp.content_key = av.content_key {AJ})"""
            rows = await conn.fetch(
                f"""
                WITH k AS (
                    SELECT av.content_key, av.pdp_lifecycle_stage AS stage,
                           CASE WHEN {survivor} THEN 'no_titled_unquarantined_member'
                                ELSE 'all_members_quarantined' END AS reason,
                           {override} AS saved_by_override
                    FROM agent_pdp_view av
                    WHERE EXISTS (SELECT 1 FROM catalog_products cp0 WHERE cp0.content_key = av.content_key)
                      AND NOT {titled}
                )
                SELECT k.content_key, k.stage, k.reason, k.saved_by_override,
                       COALESCE(bool_or(ips.serving_eligible), FALSE) AS serving_eligible,
                       COALESCE(bool_or(ips.index_eligible), FALSE) AS index_eligible,
                       (SELECT count(*) FROM agent_decision_funnel_links l
                         WHERE l.content_key = k.content_key) AS links
                FROM k LEFT JOIN index_pipeline_state ips ON ips.content_key = k.content_key
                GROUP BY k.content_key, k.stage, k.reason, k.saved_by_override
                """
            )
            total = await conn.fetchval("SELECT count(*) FROM agent_pdp_view")
            reap = [r for r in rows if not r["saved_by_override"]]
            by = lambda key: {v: sum(1 for r in reap if r[key] == v) for v in sorted({r[key] or "null" for r in reap})}
            out = {
                "view_rows": total,
                "unbuildable_without_override_arm": len(rows),
                "saved_by_title_override": sum(1 for r in rows if r["saved_by_override"]),
                "would_reap": len(reap),
                "by_reason": by("reason"),
                "by_stage": {s: sum(1 for r in reap if (r["stage"] or "null") == s)
                             for s in sorted({r["stage"] or "null" for r in reap})},
                "ips_flips_serving_or_index_eligible": sum(1 for r in reap if r["serving_eligible"] or r["index_eligible"]),
                "ips_serving_eligible": sum(1 for r in reap if r["serving_eligible"]),
                "ips_index_eligible": sum(1 for r in reap if r["index_eligible"]),
                "keys_with_funnel_links": sum(1 for r in reap if r["links"]),
                "funnel_links_total": sum(int(r["links"]) for r in reap),
                "audit_table_present": bool(present["audit"]),
                "content_keys": sorted(r["content_key"] for r in reap),
            }
            print("CENSUS " + json.dumps(out, sort_keys=True), flush=True)
        finally:
            await tr.rollback()
    finally:
        await conn.close()
    print("CENSUS_DONE", flush=True)


asyncio.run(main())
