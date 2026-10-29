"""Which listings in the catalog are not products at all? A read-only census, one job.

A donation, a gift card or voucher, an order-protection or insurance fee, a tip, a deposit. These
are real Shopify "products" on the merchant's store, so every crawl lane ingests them, and nothing
but the serving classifier's `non_core_product` rule
(`services.index_pipeline_state_service._is_non_core_product`) keeps them off the shelf. Run on
2026-10-21 because fentybeauty.com's "Donate To The Clara Lionel Foundation" was proposed as a
competitive alternative for 8 anchors by the relationship-graph rebuild.

SECTIONS (one `NP2 {...}` line each; row-level sections print one `NP2_ROW {...}` line per row,
because a whole section in one line overflows a Cloud Logging entry and reads back truncated)
  cp_match              every catalog_products row whose title or product_type matches, in ANY
                        state, with sync_status, suppression, pdp_will_render, ips serving /
                        index eligibility + blocker, trust decision, and its attached seeds.
                        `rule_blocks` is the serving rule's own verdict, imported from the
                        running image: before the rule ships it shows which rows the OLD list
                        misses, after it ships every row should read true.
  seed_match            every external_product_seeds row whose title or product_type matches,
                        with its attached catalog row's state. An ACTIVE seed is in the
                        relationship graph's seed candidate lane whatever its catalog row says.
  labels_*              relationship_candidate_labels rows that name any of those listings, as
                        candidate or as anchor, by every ref they can carry
                        (product:<signature>, product:<seed / source id>, product:<product_key>).

The SQL patterns are deliberately the census set, wider in places than the rule (they are how the
rule was calibrated: on 2026-10-21 they matched 66 catalog rows and 58 seeds, none of them a real
product). Do not "fix" them to equal the rule; `rule_blocks` is the comparison.

SAFETY. Session READ ONLY, statement_timeout 25 s, each statement runs once. pivota-pg also serves
live search: start it only while CPU is under 35% and no retailer-ingest-drain or relgraph-sync
execution is running. It fetches nothing from merchants, so it needs no crawl subnet.

    # before this file is in the deployed image (the program must stay free of the at-sign):
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/non_product_listing_census.py)"
    # once it is:
    bash scripts/ops/run_oneoff_job.sh scripts/ops/non_product_listing_census.py
"""

import json
import os
import sys

import psycopg2

sys.path.insert(0, "/app")
try:
    from services.index_pipeline_state_service import _is_non_core_product
except Exception:  # noqa: BLE001 - census still runs on an image without the module
    _is_non_core_product = None

NP = (
    r"\mdonations?\M|^\W*donate\M|\mround[- ]?up\M|gift[ -]?cards?|\me-?gift|egift|gift (certificate|voucher)s?"
    r"|store credit|(shipping|package|delivery|order|parcel|shipment) (protection|insurance|guarantee)"
    r"|\mroute (insurance|protection|package)|\mnavidium\M|\mseel\M|^\W*(add (a )?)?tips?\W*$|\mgratuity\M"
    r"|\mtip jar\M|\m(booking|pre-?order|appointment|reservation|security) deposit\M|^\W*deposit\M"
)
PT = (
    r"^\W*(gift[ _-]?cards?|e-?gift[ _-]?cards?|donations?|insurance|shipping protection|protection|tips?"
    r"|deposit|gratuity)\W*$"
)
ARGS = {"np": NP, "pt": PT}
SEED_TITLE = "coalesce(e.seed_data->'snapshot'->>'title', e.title, e.seed_data->>'title', '')"
SEED_PT = "coalesce(e.seed_data->'snapshot'->>'product_type', e.seed_data->>'product_type', '')"

CP_SQL = f"""
SELECT cp.product_key, cp.content_key, cp.merchant_id, cp.platform, cp.source_system, cp.source_domain,
       cp.source_product_id, cp.pivota_signature_id, cp.canonical_url, cp.title, cp.product_type, cp.brand,
       cp.sync_status, (cp.suppressed_at IS NOT NULL) AS suppressed, cp.pdp_will_render,
       ips.serving_eligible, ips.index_eligible, ips.blocker_code, crt.serving_decision AS trust,
       apv.title AS apv_title,
       (SELECT e.title FROM external_product_seeds e WHERE e.attached_product_key = cp.product_key
         ORDER BY e.updated_at DESC LIMIT 1) AS seed_title,
       (SELECT string_agg(e.id || ':' || e.status, ' ') FROM external_product_seeds e
         WHERE e.attached_product_key = cp.product_key) AS seeds
FROM catalog_products cp
LEFT JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
LEFT JOIN catalog_row_trust crt ON crt.subject_type = 'product' AND crt.subject_key = cp.product_key
LEFT JOIN agent_pdp_view apv ON apv.content_key = cp.content_key
WHERE cp.title ~* %(np)s OR coalesce(cp.product_type, '') ~* %(pt)s
ORDER BY cp.source_domain, cp.title LIMIT 2000"""

SEED_SQL = f"""
SELECT e.id, e.external_product_id, e.domain, e.market, e.status, {SEED_TITLE} AS title,
       {SEED_PT} AS product_type, e.attached_product_key, cp.sync_status AS cp_sync,
       (cp.suppressed_at IS NOT NULL) AS cp_suppressed, cp.pdp_will_render AS cp_will_render
FROM external_product_seeds e LEFT JOIN catalog_products cp ON cp.product_key = e.attached_product_key
WHERE {SEED_TITLE} ~* %(np)s OR {SEED_PT} ~* %(pt)s
ORDER BY e.domain, e.id LIMIT 2000"""

LABELS_AS_CANDIDATE_SQL = """
SELECT l.relation_type, l.label_state, count(*) AS labels, count(DISTINCT l.anchor_ref) AS anchors,
       count(DISTINCT l.candidate_product_ref) AS candidates,
       count(*) FILTER (WHERE l.last_verified_at IS NOT NULL AND l.expires_at > now()) AS verified_unexpired
FROM relationship_candidate_labels l WHERE lower(l.candidate_product_ref) = ANY(%(refs)s)
GROUP BY 1, 2 ORDER BY 3 DESC"""

LABELS_BY_CANDIDATE_SQL = """
SELECT l.candidate_product_ref, l.relation_type, l.label_state, count(*) AS labels,
       count(DISTINCT l.anchor_ref) AS anchors,
       count(*) FILTER (WHERE l.last_verified_at IS NOT NULL AND l.expires_at > now()) AS verified_unexpired,
       max(l.created_at) AS newest
FROM relationship_candidate_labels l WHERE lower(l.candidate_product_ref) = ANY(%(refs)s)
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC"""

LABELS_AS_ANCHOR_SQL = """
SELECT l.anchor_ref, l.relation_type, l.label_state, count(*) AS labels
FROM relationship_candidate_labels l WHERE lower(l.anchor_ref) = ANY(%(refs)s)
GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 100"""


def _plain(v):
    return v if isinstance(v, (int, float, str, type(None), bool)) else str(v)


def main() -> int:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(dsn.replace("sslmode=no-verify", "sslmode=require"))
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '25s'")

    def q(name, sql, args, each=False):
        cur.execute(sql, args)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, map(_plain, r))) for r in cur.fetchall()]
        if each:
            print("NP2 " + json.dumps({"q": name, "n": len(rows)}))
            for r in rows:
                print("NP2_ROW " + json.dumps({"q": name, **r}))
        else:
            print("NP2 " + json.dumps({"q": name, "n": len(rows), "rows": rows}))
        return rows

    cur.execute(CP_SQL, ARGS)
    cols = [c[0] for c in cur.description]
    cp = [dict(zip(cols, map(_plain, r))) for r in cur.fetchall()]
    print("NP2 " + json.dumps({"q": "cp_match", "n": len(cp), "rule_imported": _is_non_core_product is not None}))
    for r in cp:
        if _is_non_core_product is not None:
            r["rule_blocks"] = _is_non_core_product(
                {"seed_title": r["seed_title"], "pdp_title": r["apv_title"], "canonical_url": r["canonical_url"],
                 "product_type": r["product_type"]},
                seed_title=r["seed_title"] or "",
            )
        print("NP2_ROW " + json.dumps({"q": "cp_match", **r}))

    seeds = q("seed_match", SEED_SQL, ARGS, each=True)

    refs = set()
    for r in cp:
        for v in (r["pivota_signature_id"], r["source_product_id"], r["product_key"]):
            if v:
                refs.add(("product:" + v).lower())
    for r in seeds:
        for v in (r["id"], r["external_product_id"]):
            if v:
                refs.add(("product:" + v).lower())
    refs = sorted(refs)
    print("NP2 " + json.dumps({"q": "refs", "n": len(refs)}))
    q("labels_as_candidate", LABELS_AS_CANDIDATE_SQL, {"refs": refs})
    q("labels_by_candidate", LABELS_BY_CANDIDATE_SQL, {"refs": refs})
    q("labels_as_anchor", LABELS_AS_ANCHOR_SQL, {"refs": refs})
    print("NP2_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
