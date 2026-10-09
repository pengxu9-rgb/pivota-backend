#!/usr/bin/env python3
"""Retire catalog rows whose product_key was superseded by the brand-attribution fix.

REVERSIBLE tombstone, never a delete — the precedent set by
scripts/suppress_external_seed_rig_rows.py and scripts/merge_duplicate_canonicals.py:
`catalog_products.suppression_reason` + `suppressed_at` + `suppression_metadata`
carrying the run id, plus `external_product_seeds.status='inactive'` and the offer
cascade. `revert` undoes a whole run from its manifest.

WHY THIS EXISTS. `derive_product_key` hashes `(brand, title)`, so #2173 — which stopped
`brand_override` relabelling a sibling brand — MOVES the key of every product whose brand
it corrected. On misshaus.com that is 27 of 125 rows (A'pieu 17, CHOGONGJIN 7, Time
Revolution 2, Glow Skin 1). Re-onboarding writes the corrected rows under NEW keys and
`apply_ingest_plan` upserts on `product_key`, so it has no way to retire the old ones:
without this step the storefront ends up with 27 duplicate products, one of each pair
wrong-branded. `make_content_key` also hashes the brand, so the two rows of a pair do not
share a content_key and the duplicate-merge pipeline will never pair them either.

THE COHORT IS RE-DERIVED, NEVER PASSED IN. It comes from `records_for_brand` — the SAME
call the re-onboard makes — so the set retired here cannot drift from the set superseded
there. A key whose brand the fix did NOT move is not in the cohort by construction.

ORDER. Run this AFTER the re-onboard has applied: a stale key is retired only once its NEW key is
live (review of #2397 -- the drain's re-run drops unresolved rows, excluded handles and refused plan
rows, and a job can sit held for review; measured 2026-09-27, 72 of stilacosmetics.com's 125 records
were unresolved, so retiring first would have hidden them). Both rows of a pair serving for a while is
the status quo; a missing product is not. --before-rewrite restores the old order explicitly.
Live is not served: a stale key whose old row is serving_eligible is also kept while its new key's
content_key is not (e.g. blocked on short_description) -- reported as NEW NOT SERVING, retired on a later
run once the new row serves.

(Formerly: run this BEFORE the re-onboard.) The two key sets are disjoint, so a suppressed
stale SKU cannot collide with a new one (`_SKU_SUPPRESSED_IDENTITY_SQL` guards on a
matching `product_key`, and ours differ) — but retiring first means the storefront is
never simultaneously serving both rows of a pair.

THE DRAIN RUNS THIS TOO. A retailer-ingest job with options.retire_stale_brand (a brand_official storefront
re-run under its canonical spelling) retires the old keys itself right after its verified apply, with this
file's own pieces -- cohort_from_records (on the records the apply wrote), plan_for_cohort, prepare_retire,
write_retire -- and stores the manifest on the apply run before writing. Undo it with
`revert --ingest-run rir_...`. The CLI below stays for stores outside the drain and for a deferred retire.

DRY-RUN BY DEFAULT. Nothing is written without --apply.

  # 1. plan
  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      --domain misshaus.com --brand Missha --category beauty/skincare

  # 2. apply (one transaction; writes a reversal manifest)
  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      --domain misshaus.com --brand Missha --category beauty/skincare \
      --apply --manifest /tmp/retire_misshaus.json

  # a store written under another spelling and re-run under the canonical one:
  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      --domain tartecosmetics.com --brand Tarte --stale-brand "Tarte Cosmetics" --category beauty

  # 3. undo the whole run
  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      revert --manifest /tmp/retire_misshaus.json

TRUST. A tombstone is invisible to discovery only once the row's catalog_row_trust is recomputed
(catalog_trust_policy reads any suppression_reason as blocked ROW_TOMBSTONED); until then the gateway's public
discovery, entity feed and sitemap keep serving it on its old `public` decision. So `write_retire` and
`revert_manifest` recompute trust for exactly the keys they changed, right after their transaction commits
(measured 2026-09-28: retire_b9cd3948eef2 tombstoned 26 Tower 28 rows at 14:02Z, their trust caught up at
18:19Z on the 6-hourly backfill cron). A trust refresh that fails is printed and logged at ERROR and returned in
the counts; re-run it alone with

  DATABASE_URL=... python3 scripts/retire_superseded_brand_keys.py \
      refresh-trust --manifest /tmp/retire_misshaus.json      # or --ingest-run rir_...

`refresh-trust` needs no --apply: it changes no catalog row, only recomputes the derived trust rows from the rows
as they are (idempotent, what the backfill cron does on its own schedule). Run it again AFTER
services.catalog_offer_suppression.revert_offer_suppression when undoing a run: a restored row whose offers are
still suppressed is recomputed blocked (no priced offer) by the revert itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.apply import live_retailer_listing_owner  # noqa: E402
from services.catalog_enrichment_agent.ingestion import derive_product_key  # noqa: E402
from services.catalog_offer_suppression import (  # noqa: E402
    cascade_for_suppressed_product_keys,
)
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many  # noqa: E402
from services.curated_brand_feed import records_for_brand  # noqa: E402

logger = logging.getLogger(__name__)

REASON = "brand_attribution_key_supersede"

LIVE_ROWS_SQL = """
SELECT product_key, merchant_id, brand, title, source_domain, content_key, suppression_reason, suppressed_at,
       suppression_metadata
FROM catalog_products
WHERE product_key = ANY(:keys)
"""

SUPPRESS_SQL = """
UPDATE catalog_products
SET suppression_reason = :reason,
    suppressed_at = COALESCE(suppressed_at, NOW()),
    suppression_metadata = CAST(:metadata AS jsonb),
    updated_at = NOW()
WHERE product_key = ANY(:keys)
  AND suppression_reason IS NULL
"""

# Serving is decided per content_key, not per product_key: a row is on the storefront only while its
# content_key is serving_eligible. A content_key with no state row, or a NULL flag, is not serving. The flag
# is read, not filtered on, so `plan` decides it in code the tests exercise.
SERVING_SQL = """
SELECT content_key, serving_eligible FROM index_pipeline_state
WHERE content_key = ANY(:keys)
"""

# Search is a second surface with its own gate (review of #2426): public recall and discovery read the ROW's
# catalog_row_trust.serving_decision = 'public' (services/catalog_trust_policy.py), and backend global recall
# admits only pdp_lifecycle_stage validated/published or NULL (services/pivot_query_service.py; the same list
# as retailer_ingest.pipeline.BACKEND_RECALL_LIFECYCLE_STAGES). A new row that serves its page but lands as
# `candidate` or trust-shadowed is not findable. A row with no trust row is not searchable.
# The PRODUCT's own trust row, joined the way search joins it (PIVOTA-Agent catalogServingIndex.js:
# subject_type = 'product' AND subject_key = product_key). catalog_row_trust also holds offer / listing /
# content_key rows whose nullable product_key names the product (mig 136); a public one of those must not
# make an unsearchable product look searchable (queue review of #2426).
SEARCHABLE_SQL = """
SELECT p.product_key FROM catalog_products p
JOIN catalog_row_trust t ON t.subject_type = 'product' AND t.subject_key = p.product_key
WHERE p.product_key = ANY(:keys) AND t.serving_decision = 'public'
  AND (p.pdp_lifecycle_stage IS NULL OR p.pdp_lifecycle_stage IN ('validated', 'published'))
"""

SEEDS_FOR_KEYS_SQL = """
SELECT id, status FROM external_product_seeds
WHERE attached_product_key = ANY(:keys)
"""

DEACTIVATE_SEEDS_SQL = """
UPDATE external_product_seeds
SET status = 'inactive', updated_at = NOW()
WHERE attached_product_key = ANY(:keys)
  AND lower(coalesce(status, '')) = 'active'
RETURNING id
"""

URLS_FOR_KEYS_SQL = """
SELECT product_key, canonical_url FROM catalog_products WHERE product_key = ANY(:keys)
"""

UNSUPPRESS_SQL = """
UPDATE catalog_products
SET suppression_reason = :reason, suppressed_at = CAST(:suppressed_at AS timestamptz),
    suppression_metadata = CAST(:metadata AS jsonb), updated_at = NOW()
WHERE product_key = :key
  AND suppression_reason = :retired_reason AND suppression_metadata ->> 'run_id' = :run_id
RETURNING product_key
"""

# Only a seed on a row this revert restored: a row retired again since (another run) keeps its seed off.
REACTIVATE_SEED_SQL = """
UPDATE external_product_seeds SET status = :status, updated_at = NOW()
WHERE id = :id AND attached_product_key = ANY(:keys)
"""


# The manifest keys that still carry THIS run's tombstone (not reverted, not re-retired by another run).
STILL_RETIRED_SQL = """
SELECT product_key FROM catalog_products
WHERE product_key = ANY(:keys) AND suppression_reason = :reason AND suppression_metadata ->> 'run_id' = :run_id
"""

# A retired key whose PRODUCT trust row still reads public after the refresh: the surfaces that gate on
# serving_decision = 'public' (PIVOTA-Agent catalogServingIndex) would still list it. Joined the way they join.
PUBLIC_TRUST_SQL = """
SELECT subject_key FROM catalog_row_trust
WHERE subject_type = 'product' AND subject_key = ANY(:keys) AND serving_decision = 'public'
"""


def _as_json_text(value: Any) -> Optional[str]:
    """A jsonb bind value as TEXT, or None. See the note in `revert`."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


async def build_cohort(domain: str, brand: str, category_path: str,
                       stale_brand: Optional[str] = None) -> List[Dict[str, Any]]:
    """(stale_key, new_key, brand, title) for every record the fix re-keyed.

    `emit_real_variants=True` mirrors the re-onboard invocation; it does not affect the
    key, but keeping the two calls identical is what makes the cohorts provably the same.

    `stale_brand`: the spelling the OLD rows were written under, when it is not the re-run's brand. A
    store once written as "Tarte Cosmetics" / "Stila Cosmetics" / "Tower 28 Beauty" and re-run as
    "Tarte" / "Stila" / "Tower 28" moves every key from derive(stale_brand, title) to
    derive(record brand, title); without it the stale key is derived from the re-run's own brand and
    the cohort comes back empty (the Sand & Sky case, 2026-09-26). Keys that were never written under
    the stale spelling are simply absent from catalog_products, and `plan` never retires an absent key.
    """
    recs = await records_for_brand(
        domain=domain, category_path=category_path, brand=brand, emit_real_variants=True
    )
    return cohort_from_records(recs, brand, stale_brand)


def cohort_from_records(recs: List[Dict[str, Any]], brand: str,
                        stale_brand: Optional[str] = None) -> List[Dict[str, Any]]:
    """Pure: the cohort of `build_cohort` from records already crawled. The retailer-ingest drain passes the
    records its apply stage just wrote, so the cohort is the re-run's own crawl -- not a second one."""
    pairs = []
    for rec in recs:
        pdp = rec.get("pdp") or {}
        title, new_brand = pdp.get("product_name"), pdp.get("brand")
        stale, new = derive_product_key(stale_brand or brand, title), derive_product_key(new_brand, title)
        if stale and new and stale != new:
            pairs.append({"stale_key": stale, "new_key": new, "brand": new_brand, "title": title})
    # A stale key that is ANOTHER record's new key is that product's current row, never an old one: the key
    # hashes (brand, title) run together, so a sibling brand ("A'pieu Pure Block Sun" re-keyed while "Missha
    # Pure Block Sun" is written under the stale spelling) or a split ("Tower 28 Beauty" + "Lip Jelly" ==
    # "Tower 28" + "Beauty Lip Jelly") collides with a live product of the same run (review of #2426).
    current = {p["new_key"] for p in pairs}
    out: List[Dict[str, Any]] = []
    seen = set()
    for p in pairs:
        if p["stale_key"] in current or p["stale_key"] in seen:
            continue  # ...and one stale key is one row: variants and repeated titles are not two retires
        seen.add(p["stale_key"])
        out.append(p)
    return out


def _host(value: Optional[str]) -> str:
    return str(value or "").strip().lower().removeprefix("www.")


def select_retirable(cohort: List[Dict[str, Any]], rows: Dict[str, Dict[str, Any]], new_live: set,
                     domain: str, *, serving: set, searchable: set,
                     before_rewrite: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """Pure: split the cohort into what may be tombstoned and why the rest may not.

    A stale key is retirable only when it is present and live, owned by THIS store (derive_product_key
    hashes (brand, title) only, so the same key can belong to another source -- never touch it), and,
    unless before_rewrite, its new key is already live (the re-run actually wrote the product).

    `serving`: the product_keys (stale and new) whose content_key is serving_eligible. Required, so no
    caller can skip the check. A live new key is not enough when the old row is the one on the storefront:
    if the new row is blocked (short_description, ...) retiring the old row takes a served product off the
    catalog (hand-checked per store 2026-09-28). Such a key is kept as `new_not_serving`. An old row that is
    not serving loses nothing and is still retired. before_rewrite skips this with the new-key-live check:
    the old order accepts the gap explicitly.

    `searchable`: the same rule for search (public recall/discovery; SEARCHABLE_SQL), which gates on the ROW's
    trust and lifecycle, not the page's content_key (review of #2426: the O HUI rows landed page-served but
    `candidate`). Required as well. A key is kept when the retire would lose EITHER surface the old row has."""
    host = _host(domain)
    present = [c for c in cohort if c["stale_key"] in rows]
    suppressed = [c for c in present if rows[c["stale_key"]].get("suppression_reason")]
    live = [c for c in present if c not in suppressed]
    foreign = [c for c in live if _host(rows[c["stale_key"]].get("source_domain")) != host]
    own = [c for c in live if c not in foreign]
    waiting = [] if before_rewrite else [c for c in own if c["new_key"] not in new_live]
    new_not_serving = [] if before_rewrite else [
        c for c in own if c not in waiting and (
            (c["stale_key"] in serving and c["new_key"] not in serving)
            or (c["stale_key"] in searchable and c["new_key"] not in searchable))
    ]
    retire = [c for c in own if c not in waiting and c not in new_not_serving]
    return {"present": present, "live": retire, "foreign": foreign, "waiting_for_new_key": waiting,
            "new_not_serving": new_not_serving, "already_suppressed": suppressed}


async def load_serving(stale_rows: Dict[str, Dict[str, Any]], own_live_new_rows: List[Dict[str, Any]]) -> set:
    """The `serving` set for `select_retirable`: product_keys whose content_key is serving_eligible.

    One query over both sides' content_keys: the new key's serving state decides whether a retire loses a
    product, the old row's whether there is anything to lose. Pass only THIS store's live new rows -- a
    foreign or suppressed row under the new key is not the re-run's row and must not make it look served."""
    ck_of = {r["product_key"]: r.get("content_key")
             for r in [*stale_rows.values(), *own_live_new_rows] if r.get("content_key")}
    if not ck_of:
        return set()
    serving_cks = {
        r["content_key"] for r in await database.fetch_all(SERVING_SQL, {"keys": sorted(set(ck_of.values()))})
        if r["serving_eligible"] is True
    }
    return {k for k, ck in ck_of.items() if ck in serving_cks}


async def load_searchable(keys: List[str]) -> set:
    """The `searchable` set for `select_retirable`: product_keys a public search can return (SEARCHABLE_SQL)."""
    if not keys:
        return set()
    return {r["product_key"] for r in await database.fetch_all(SEARCHABLE_SQL, {"keys": sorted(set(keys))})}


async def plan(domain: str, brand: str, category_path: str,
               stale_brand: Optional[str] = None, *, before_rewrite: bool = False) -> Dict[str, Any]:
    cohort = await build_cohort(domain, brand, category_path, stale_brand)
    return await plan_for_cohort(cohort, domain, brand, category_path, stale_brand, before_rewrite=before_rewrite)


async def plan_for_cohort(cohort: List[Dict[str, Any]], domain: str, brand: str, category_path: str,
                          stale_brand: Optional[str] = None, *, before_rewrite: bool = False) -> Dict[str, Any]:
    """`plan` for a cohort already built (the CLI crawls; the drain passes the records it applied)."""
    stale_keys = [c["stale_key"] for c in cohort]
    new_keys = [c["new_key"] for c in cohort]
    rows = {r["product_key"]: dict(r) for r in await database.fetch_all(LIVE_ROWS_SQL, {"keys": stale_keys})}
    new_rows = [dict(r) for r in await database.fetch_all(LIVE_ROWS_SQL, {"keys": new_keys})]
    already_new = {r["product_key"] for r in new_rows}
    # A new key counts only when THIS store's re-run wrote it (live, own source_domain): another source's row
    # under the same (brand, title) key proves nothing about the re-run (re-review of #2397).
    new_live = {r["product_key"] for r in new_rows
                if not r.get("suppression_reason") and _host(r.get("source_domain")) == _host(domain)}
    serving = await load_serving(rows, [r for r in new_rows if r["product_key"] in new_live])
    searchable = await load_searchable([*rows, *new_live])
    split = select_retirable(cohort, rows, new_live, domain, serving=serving, searchable=searchable,
                             before_rewrite=before_rewrite)
    present, live = split["present"], split["live"]
    # Seeds and offers for the keys this run will actually retire -- never a waiting or foreign key, so the
    # plan's counts are true and revert's manifest names only seeds this run deactivates.
    retire_keys = [c["stale_key"] for c in live]
    seeds = [dict(r) for r in await database.fetch_all(SEEDS_FOR_KEYS_SQL, {"keys": retire_keys})] if retire_keys else []
    offers = await cascade_for_suppressed_product_keys(retire_keys, apply=False) if retire_keys else []
    return {
        "foreign": split["foreign"], "waiting_for_new_key": split["waiting_for_new_key"],
        "new_not_serving": split["new_not_serving"], "already_suppressed": split["already_suppressed"],
        # product_keys (old and new) whose content_key serves, as read BEFORE any write: the drain's read-back
        # checks a retired key's new row still serves wherever its old row did.
        "serving": sorted(serving),
        "searchable": sorted(searchable),
        "domain": domain, "brand_override": brand, "category_path": category_path, "stale_brand": stale_brand,
        "cohort": cohort, "rows": rows, "present": present, "live": live,
        "already_new": sorted(already_new), "seeds": seeds,
        "active_seeds": [s for s in seeds if str(s.get("status") or "").lower() == "active"],
        "offers": offers,
    }


def print_plan(p: Dict[str, Any]) -> None:
    print(f"domain            : {p['domain']}  (brand override {p['brand_override']!r})")
    if p.get("stale_brand"):
        print(f"stale spelling    : {p['stale_brand']!r}  (old rows' keys derive from it)")
    print(f"re-keyed          : {len(p['cohort'])}")
    print(f"  present in catalog_products : {len(p['present'])}")
    print(f"  LIVE (would be tombstoned)  : {len(p['live'])}")
    print(f"  WAITING (new key not live)  : {len(p.get('waiting_for_new_key') or [])}  -- never retired")
    print(f"  NEW NOT SERVING (old served): {len(p.get('new_not_serving') or [])}  -- never retired")
    print(f"  FOREIGN (another source)    : {len(p.get('foreign') or [])}  -- never retired")
    print(f"  already suppressed          : {len(p.get('already_suppressed') or [])}")
    print(f"  absent from catalog         : {len(p['cohort']) - len(p['present'])}")
    print(f"seeds attached    : {len(p['seeds'])}  (active, would deactivate: {len(p['active_seeds'])})")
    print(f"offers to cascade : {len(p['offers'])}")
    if p["already_new"]:
        print(f"NOTE: {len(p['already_new'])} replacement key(s) ALREADY present — a re-onboard has run.")
    print()
    for c in p["live"][:40]:
        print(f"  {c['brand']:<16} {c['title'][:46]}")
        print(f"      retire : {c['stale_key']}")
        print(f"      keeps  : {c['new_key']}")


def prepare_retire(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The run id, the rows' suppression metadata and the reversal manifest for `p`'s retirable keys, or
    None when there is nothing to retire. Pure; the caller stores the manifest durably BEFORE `write_retire`
    (a manifest written after the write cannot describe what it replaced)."""
    keys = [c["stale_key"] for c in p["live"]]
    if not keys:
        return None
    run_id = f"retire_{uuid.uuid4().hex[:12]}"
    metadata = json.dumps({
        "run_id": run_id, "reason": REASON,
        "pr": "pivota-backend#2173" if not p.get("stale_brand") else "stale-brand re-key",
        "domain": p["domain"], "brand_override": p["brand_override"], "stale_brand": p.get("stale_brand"),
        "note": ("product_key superseded when the brand override stopped relabelling a sibling brand"
                 if not p.get("stale_brand") else
                 f"product_key superseded: rows written as {p['stale_brand']!r} re-run as {p['brand_override']!r}"),
        "at": datetime.now(timezone.utc).isoformat(),
    })
    # BEFORE-state first: a manifest written after the write cannot describe what it replaced.
    manifest = {
        "run_id": run_id, "reason": REASON, "domain": p["domain"],
        "brand_override": p["brand_override"], "category_path": p["category_path"],
        "stale_brand": p.get("stale_brand"),
        "at": datetime.now(timezone.utc).isoformat(),
        "products": [
            {
                "product_key": k,
                "prior_suppression_reason": p["rows"][k].get("suppression_reason"),
                "prior_suppressed_at": (
                    p["rows"][k]["suppressed_at"].isoformat()
                    if p["rows"][k].get("suppressed_at") else None
                ),
                "prior_suppression_metadata": p["rows"][k].get("suppression_metadata"),
            } for k in keys
        ],
        "seeds": [{"id": str(s["id"]), "prior_status": s.get("status")} for s in p["active_seeds"]],
    }
    return {"run_id": run_id, "keys": keys, "metadata": metadata, "manifest": manifest}


async def write_retire(prepared: Dict[str, Any]) -> Dict[str, Any]:
    """The tombstone write, one transaction: rows, their active seeds, the offer cascade. Raises (rolled back)
    unless every key ends suppressed. Then, after the commit, the retired rows' trust (`refresh_trust`)."""
    keys = prepared["keys"]
    async with database.transaction():
        await database.execute(SUPPRESS_SQL, {"reason": REASON, "metadata": prepared["metadata"], "keys": keys})
        seed_rows = await database.fetch_all(DEACTIVATE_SEEDS_SQL, {"keys": keys})
        offer_ids = await cascade_for_suppressed_product_keys(keys, apply=True)
        after = await database.fetch_all(LIVE_ROWS_SQL, {"keys": keys})
        unsuppressed = [r["product_key"] for r in after if not r["suppression_reason"]]
        if unsuppressed:
            raise RuntimeError(f"tombstone did not land on {len(unsuppressed)} row(s): {unsuppressed[:5]}")
    return {"products": len(keys), "seeds": len(seed_rows), "offers": len(offer_ids),
            **await refresh_trust(keys, run_id=prepared["run_id"], after="retire", retired=keys)}


async def refresh_trust(keys: List[str], *, run_id: str, after: str, retired: List[str]) -> Dict[str, Any]:
    """Recompute catalog_row_trust for exactly the rows a run just tombstoned (or restored), with the backend's
    own upserter so the policy is not restated here.

    AFTER the commit, never inside it: a trust statement that fails inside the transaction aborts it, and the
    upserter swallows the error -- so the COMMIT would quietly roll the retire back. And never raises: the retire
    has committed, and the drain reads a raise from `write_retire` as "nothing retired"
    (services.retailer_ingest.pipeline._retire_stale_brand). Matches withdraw_catalog_rows and brand_relabel,
    which refresh trust after their write as well.

    Loud instead: a key the upserter did not rewrite, or a `retired` key still `public`, is printed, logged at
    ERROR and returned as `trust_problems` (the drain fails the job on it). The rows then keep their old decision
    until `refresh-trust` is re-run or jobs/catalog_row_trust_backfill_cron.py reaches them."""
    out: Dict[str, Any] = {"trust": 0}
    if not keys:
        return out
    problems: List[str] = []
    try:
        out["trust"] = int(await upsert_catalog_row_trust_many(db=database, product_keys=keys) or 0)
        if out["trust"] < len(keys):
            problems.append(f"{len(keys) - out['trust']} of {len(keys)} key(s) not rewritten")
        if retired:
            still = sorted(r["subject_key"] for r in await database.fetch_all(PUBLIC_TRUST_SQL, {"keys": retired}))
            if still:
                problems.append(f"{len(still)} retired key(s) still public: {still[:5]}")
    except Exception as exc:  # noqa: BLE001 -- the write committed; a trust failure is reported, never raised
        problems.append(f"{type(exc).__name__}: {exc}"[:300])
    if problems:
        out["trust_problems"] = problems
        msg = (f"catalog_row_trust refresh FAILED after {after} {run_id}: "
               f"{'; '.join(problems)}. The rows keep their old trust decision until "
               f"`retire_superseded_brand_keys.py refresh-trust` is re-run for this run, or the backfill cron "
               f"reaches them.")
        logger.error(msg)
        print(f"  ! {msg}", file=sys.stderr)
    return out


async def apply(p: Dict[str, Any], manifest_path: str) -> Dict[str, Any]:
    prepared = prepare_retire(p)
    if not prepared:
        print("nothing live to retire — no write.")
        return {}
    run_id, manifest = prepared["run_id"], prepared["manifest"]
    Path(manifest_path).write_text(json.dumps(manifest, indent=1, default=str))
    print(f"manifest written BEFORE the write: {manifest_path}")
    # AND to stdout. This script's normal home is a Cloud Run Job, whose filesystem dies
    # with the container — a manifest that exists only at `manifest_path` is gone the
    # moment the job ends, which is to say the run is not actually revertible. Printing it
    # puts the reversal record in Cloud Logging, where it outlives the container. Bounded:
    # one small object per retired key.
    print("----8<---- MANIFEST BEGIN ----8<----")
    print(json.dumps(manifest, default=str))
    print("----8<---- MANIFEST END ----8<----")
    counts = await write_retire(prepared)
    print(f"applied: {counts}  run_id={run_id}")
    return counts


# The manifest the retailer-ingest drain stored on its apply run (services.retailer_ingest.pipeline).
INGEST_RUN_MANIFEST_SQL = """
SELECT checks -> 'stale_brand_retire_manifest' AS manifest,
       checks -> 'stale_brand_retire' ->> 'outcome' AS outcome
FROM retailer_ingest_runs WHERE id = :id
"""
#: A drain retire that never reached its write (services.retailer_ingest.pipeline._retire_stale_brand).
UNWRITTEN_OUTCOMES = ("nothing_to_retire", "deferred", "error")


async def revert(manifest_path: str) -> None:
    await revert_manifest(json.loads(Path(manifest_path).read_text()))


async def revert_ingest_run(ingest_run_id: str) -> None:
    """Revert the old-spelling retire a drain apply run did, from the manifest it stored before writing."""
    await revert_manifest(await ingest_run_manifest(ingest_run_id))


async def ingest_run_manifest(ingest_run_id: str) -> Dict[str, Any]:
    """The manifest a drain apply run stored before its retire wrote; exits when that retire never wrote."""
    row = await database.fetch_one(INGEST_RUN_MANIFEST_SQL, {"id": ingest_run_id})
    m = row and row["manifest"]
    if isinstance(m, str):
        m = json.loads(m)
    if not m:
        raise SystemExit(f"no stale-brand retire manifest on ingest run {ingest_run_id}")
    if row["outcome"] in UNWRITTEN_OUTCOMES:
        raise SystemExit(f"ingest run {ingest_run_id}'s retire was {row['outcome']!r}: it wrote nothing to revert")
    return m


async def refresh_trust_for_manifest(m: Dict[str, Any]) -> Dict[str, Any]:
    """Re-run only the trust refresh for a run's keys -- the follow-up a failed `refresh_trust` asks for, and the
    last step of a revert (after revert_offer_suppression). Every key the manifest names, retired or since
    restored: the upserter derives each from the row as it is now. The keys that still carry this run's tombstone
    must end non-public, the same check `write_retire` makes."""
    keys = [row["product_key"] for row in m["products"]]
    retired = [r["product_key"] for r in await database.fetch_all(STILL_RETIRED_SQL, {
        "keys": keys, "reason": m.get("reason") or REASON, "run_id": m["run_id"]})]
    out = await refresh_trust(keys, run_id=m["run_id"], after="refresh-trust", retired=retired)
    print(f"trust {'refresh FAILED' if out.get('trust_problems') else 'refreshed'} for run {m['run_id']} "
          f"({len(retired)} of {len(keys)} key(s) still retired by it): {out}")
    return out


async def revert_manifest(m: Dict[str, Any]) -> None:
    """Restore the rows THIS run retired -- only while they still carry its tombstone (reason and run id), so
    a revert never undoes a later retire of the same key -- and reactivate the seeds on the rows it restored."""
    restored: List[str] = []
    # A retired row whose URL a retailer listing was since admitted onto (apply.legacy_chain_retired): reviving
    # it -- or its seeds -- would put two live listings on one URL. Retire that listing first, then revert.
    owned: Dict[str, str] = {}
    for r in await database.fetch_all(URLS_FOR_KEYS_SQL, {"keys": [row["product_key"] for row in m["products"]]}):
        owner = await live_retailer_listing_owner(database, r["canonical_url"])
        if owner and owner != r["product_key"]:
            owned[r["product_key"]] = owner
    async with database.transaction():
        for row in m["products"]:
            if row["product_key"] in owned:
                print(f"  ! {row['product_key']}: its URL is now {owned[row['product_key']]}'s live retailer "
                      "listing, not reverting")
                continue
            back = await database.fetch_one(UNSUPPRESS_SQL, {
                "key": row["product_key"], "reason": row["prior_suppression_reason"],
                "suppressed_at": row["prior_suppressed_at"],
                # `CAST(:metadata AS jsonb)` wants TEXT. The driver hands a jsonb column
                # back as a dict, the manifest round-trips it as one, and binding a dict
                # to a text cast fails -- in `revert`, which is the one path that must
                # not fail. Serialise anything that is not already a string.
                "metadata": _as_json_text(row["prior_suppression_metadata"]),
                "retired_reason": m.get("reason") or REASON, "run_id": m["run_id"],
            })
            if back:
                restored.append(row["product_key"])
        for s in m.get("seeds") or []:
            await database.execute(REACTIVATE_SEED_SQL, {"id": s["id"], "status": s["prior_status"],
                                                         "keys": restored})
    # After the commit, the rows it restored only (see refresh_trust): a key left alone keeps its trust row.
    trust = await refresh_trust(restored, run_id=m["run_id"], after="revert", retired=[])
    skipped = len(m["products"]) - len(restored) - len(owned)
    print(f"reverted run {m['run_id']}: {len(restored)} product(s)"
          + (f" ({skipped} no longer carry this run's tombstone, left alone)" if skipped else "")
          + (f" ({len(owned)} skipped: a live listing owns the URL)" if owned else "")
          + f", seeds on those rows, trust on {trust['trust']}. "
          "Offer suppression is reverted by services.catalog_offer_suppression.revert_offer_suppression; until it "
          "runs the restored rows' trust stays blocked (no priced offer). After it, re-run "
          f"`retire_superseded_brand_keys.py refresh-trust` for run {m['run_id']}.")


async def run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        if args.command == "refresh-trust":
            m = (await ingest_run_manifest(args.ingest_run) if args.ingest_run
                 else json.loads(Path(args.manifest).read_text()))
            return 1 if (await refresh_trust_for_manifest(m)).get("trust_problems") else 0
        if args.command == "revert":
            if args.ingest_run:
                await revert_ingest_run(args.ingest_run)
            else:
                await revert(args.manifest)
            return 0
        p = await plan(args.domain, args.brand, args.category, args.stale_brand,
                       before_rewrite=args.before_rewrite)
        print_plan(p)
        if not args.apply:
            print("\nDRY-RUN — re-run with --apply (and --manifest) to tombstone.")
            return 0
        if not args.manifest:
            print("--apply requires --manifest (the reversal record)", file=sys.stderr)
            return 2
        await apply(p, args.manifest)
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="plan", choices=["plan", "revert", "refresh-trust"])
    p.add_argument("--domain")
    p.add_argument("--brand")
    p.add_argument("--category", default="beauty/skincare")
    p.add_argument("--before-rewrite", dest="before_rewrite", action="store_true",
                   help="retire stale keys even when their new key is not live yet (the old order)")
    p.add_argument("--stale-brand", dest="stale_brand",
                   help="the spelling the OLD rows were written under, when the re-run uses another")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--manifest")
    p.add_argument("--ingest-run", dest="ingest_run",
                   help="revert / refresh-trust: the retailer-ingest apply run (rir_...) whose old-spelling retire "
                        "to act on")
    a = p.parse_args(argv)
    if a.command == "plan" and not (a.domain and a.brand):
        p.error("--domain and --brand are required")
    if a.command != "plan" and not (a.manifest or a.ingest_run):
        p.error(f"{a.command} requires --manifest or --ingest-run")
    return asyncio.run(run(a))


if __name__ == "__main__":
    raise SystemExit(main())
