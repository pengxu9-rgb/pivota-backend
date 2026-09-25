#!/usr/bin/env python3
"""Verify the URL-audit index-seeding rollout against a live DB (read-only).

Confirms the four invariants of the seed-on-audit pipeline (PR #1105):
  1. url_audit seeds exist in catalog_products and are UN-SERVED (pdp_lifecycle_stage NULL).
  2. Those seeds have NO index_pipeline_state row (so they're not recalled/searched/served).
  3. content_key collisions with real (non-audit) products are surfaced — the
     pick_canonical guardrail must keep the REAL row canonical (eyeball the served PDP).
  4. Reports for these products would emit a pipe product_key (checked via a fresh audit).

Read-only. Production is Cloud Run (pivota-prod/us-west1); Railway is the
ROLLBACK, so `railway run` here would check the canary on a platform nobody is
served from. There is no `railway run` equivalent — use a throwaway job on the
production image, which also keeps you from handling the DB secret:

    scripts/ops/run_oneoff_job.sh scripts/verify_url_audit_canary.py            # all recent seeds
    scripts/ops/run_oneoff_job.sh scripts/verify_url_audit_canary.py <merchant_id>  # one merchant

The helper mounts the DATABASE_URL secret (a job inherits NO env and NO secrets)
and takes its verdict from the job's EXIT CODE. Full pattern:
docs/runbooks/operating_on_gcp_production.md.

Reads DATABASE_PUBLIC_URL (preferred, if you have set one) or DATABASE_URL from
the environment. DATABASE_PUBLIC_URL was the Railway public proxy and is set
nowhere in production today; under the job above, DATABASE_URL is the one used.
Requires asyncpg (already a backend dependency).
"""
import asyncio
import os
import sys
from urllib.parse import urlsplit, urlunsplit, parse_qs

import asyncpg


def _dsn_and_ssl():
    raw = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
    if not raw:
        sys.exit(
            "ERROR: set DATABASE_PUBLIC_URL or DATABASE_URL "
            "(in production use `scripts/ops/run_oneoff_job.sh "
            "scripts/verify_url_audit_canary.py`, which mounts the "
            "DATABASE_URL secret — a Cloud Run job inherits nothing)."
        )
    parts = urlsplit(raw)
    # asyncpg doesn't parse sslmode from the URL — pull it out and pass ssl= kwarg.
    q = parse_qs(parts.query)
    ssl = "require" in (q.get("sslmode") or []) or parts.hostname not in ("localhost", "127.0.0.1")
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return clean, ("require" if ssl else None)


async def main():
    merchant_id = sys.argv[1] if len(sys.argv) > 1 else None
    dsn, ssl = _dsn_and_ssl()
    conn = await asyncpg.connect(dsn, ssl=ssl)
    try:
        where = "platform = 'url_audit'"
        args = []
        if merchant_id:
            where += " AND merchant_id = $1"
            args.append(merchant_id)

        # (1) seeds exist + are un-served
        seeds = await conn.fetch(
            f"""
            SELECT product_key, merchant_id, content_key, pdp_lifecycle_stage, updated_at
            FROM catalog_products
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT 25
            """,
            *args,
        )
        total = await conn.fetchval(
            f"SELECT count(*) FROM catalog_products WHERE {where}", *args
        )
        print(f"\n[1] url_audit seeds{' for '+merchant_id if merchant_id else ''}: {total} total")
        if not seeds:
            print("    (none yet — run a URL audit first; the flag must be ON at audit time)")
            return
        unserved = sum(1 for s in seeds if s["pdp_lifecycle_stage"] is None)
        print(f"    of the {len(seeds)} most recent: {unserved} un-served (pdp_lifecycle_stage NULL)")
        for s in seeds[:8]:
            print(f"      {s['product_key']}  ck={s['content_key']}  stage={s['pdp_lifecycle_stage']}")
        bad_stage = [s["product_key"] for s in seeds if s["pdp_lifecycle_stage"] is not None]
        if bad_stage:
            print(f"    !! WARNING: seeds with a non-NULL lifecycle stage (should be NULL): {bad_stage}")

        cks = [s["content_key"] for s in seeds if s["content_key"]]

        # (2) no index_pipeline_state row for pure seeds (=> not served)
        ips = await conn.fetch(
            "SELECT content_key, serving_eligible FROM index_pipeline_state WHERE content_key = ANY($1)",
            cks,
        )
        print(f"\n[2] index_pipeline_state rows for those {len(cks)} content_keys: {len(ips)}")
        if ips:
            print("    (each of these content_keys ALSO has a real serving-eligible product —")
            print("     i.e. a COLLISION; verify [3] that the real row stays canonical)")
            for r in ips:
                print(f"      ck={r['content_key']}  serving_eligible={r['serving_eligible']}")
        else:
            print("    0 — none of the seeds are serving-eligible (expected for pure seeds). ✅")

        # (3) content_key collisions with non-audit rows
        collisions = await conn.fetch(
            """
            SELECT content_key,
                   count(*) FILTER (WHERE platform = 'url_audit') AS seeds,
                   count(*) FILTER (WHERE platform <> 'url_audit') AS real_rows,
                   array_agg(DISTINCT platform) AS platforms
            FROM catalog_products
            WHERE content_key = ANY($1)
            GROUP BY content_key
            HAVING count(*) FILTER (WHERE platform <> 'url_audit') > 0
            """,
            cks,
        )
        print(f"\n[3] content_key collisions (seed shares a key with a real product): {len(collisions)}")
        if collisions:
            print("    For EACH, open the agent PDP by content_key and confirm the served")
            print("    title/description/image are the REAL product's (pick_canonical guardrail):")
            for c in collisions:
                print(f"      ck={c['content_key']}  seeds={c['seeds']}  real={c['real_rows']}  platforms={c['platforms']}")
        else:
            print("    0 — no seed collided with a real product in this sample. ✅")

        print("\nSummary: seeds are minting"
              + (" and un-served ✅" if not bad_stage else " but SOME are served ⚠️")
              + (f"; {len(collisions)} collision(s) to eyeball" if collisions else "; no collisions ✅"))
        print("Still to check in the portal: the audit result shows the 'Supply proof /")
        print("upload docs' button and a lab-report upload returns candidate claims.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
