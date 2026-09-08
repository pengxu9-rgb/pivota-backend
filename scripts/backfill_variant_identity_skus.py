"""Give external-referral products the purchasable SKU+offer their crawl already paid for.

WHAT THIS FIXES. Measured on prod 2026-09-07: of 13,799 `platform='external_seed'` products, 8,694
have no SKU carrying a merchant-issued variant id, while thousands hold such an id unread in
`seed_data.snapshot.variants`. The identity was crawled and stored; nothing projected it to the
layer that can be bought.

WHY THE PAIR. `services/catalog_variant_promoter` writes catalog_skus and deliberately not offers.
Measured the same day: **5,271 promoter-built SKUs on this track carry 0 catalog_offers rows**, while
1,583 of 1,583 ingestion-built ones carry theirs. Recall joins offers on `sku_key`
(`pivot_query_service.py:1538`), so a SKU without one is invisible to search. (An earlier draft of
this docstring said the *price gate* was the reason; that was wrong — `priced_offer_exists_sql` is
product-grain `EXISTS`, so these rows flip no `has_price` bit. Recall is the reason.)

── WHAT AN ADVERSARIAL REVIEW FOUND IN THE FIRST DRAFT, AND WHAT IT CHANGED ────────────────────
Every item below is a defect the first version of this file actually had. They are recorded because
each one is a trap the next writer of a catalog backfill will meet too.

1. `ON CONFLICT (sku_key)` DOES NOT COVER THIS TABLE. catalog_skus has TWO unique constraints, and
   Postgres infers one — it does not fall through. `idx_catalog_skus_source_identity_v2` is
   (merchant_id, platform, product_key, source_variant_id), and the promoter spells the same
   identity with a different key (`<pk>::v::<vid>` vs this lane's `<pk>::v:<vid>`). 4,971 of 16,431
   planned rows collided on the index the conflict clause did not name; none would have upserted.
   Fixed by conflicting on the IDENTITY index and taking `RETURNING sku_key`, so one identity is one
   row: where the promoter already wrote it, we adopt that row and give it the offer it never had,
   instead of minting a rival spelling of the same variant.

2. `ORDER BY (suppressed_at IS NULL) DESC` IS A PREFERENCE, NOT A FILTER. On the 573 products whose
   every offer is suppressed it returns a suppressed row, and `suppressed_at` was not among the
   INSERTed columns — so a human's withdrawal became brand-new unsuppressed priced supply under the
   withdrawn seller's attribution. Now suppressed offers are excluded in SQL, and a product with no
   live offer is skipped and counted.

3. THE SEED JOIN WAS UNSCOPED. `ON eps.external_product_id = cp.source_product_id` matched 652 seeds
   whose `attached_product_key` names a DIFFERENT product and 1,660 that are not active. A real
   merchant variant id borrowed from the wrong product still classifies MERCHANT_ISSUED — this is
   the one failure `services/variant_identity` structurally cannot catch, because it arrives through
   the join rather than the string.

4. THE OFFER ID MOVED BETWEEN RUNS. It was derived from whichever offer the ORDER BY picked, and 631
   products tie with no tiebreaker, so a re-run wrote a SECOND offer rather than upserting. It is now
   derived from the destination URL, which is a property of the product, not of a row ordering.

Also fixed: the destination was never actually carried (every offer would have been a dead end, and
the old comment claiming otherwise was false); availability was written as raw crawl text, so
"Out of Stock" did not read as out of stock; a `--allow-inherited-price` flag was parsed, threaded
and never read.

── WHAT IT STILL REFUSES TO DO ─────────────────────────────────────────────────────────────────
It will not promote an id we minted ourselves (`services/variant_identity`), will not let a variant
inherit the product's price, will not touch a suppressed product or offer, will not write a variant
whose currency disagrees with the offer it attaches to, and will not guess a seller for a product
whose live offers come from more than one merchant. Every refusal is counted in the report and in
`writer_audit_log`; none is silent.

    python3 scripts/backfill_variant_identity_skus.py                    # dry run, no token needed
    python3 scripts/backfill_variant_identity_skus.py --apply --limit 50 \
        --expect-contract backfill-v2-identity-index
    python3 scripts/backfill_variant_identity_skus.py --apply --after ext:foo::abc123 \
        --expect-contract backfill-v2-identity-index

`--apply` refuses to run without the contract token, so a stale image fails on the argument
rather than silently running the merged first draft. See CONTRACT below.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.ingestion import (  # noqa: E402
    OFFER_CATALOG_TRACK,
    OFFER_MODE,
    OFFER_READINESS_TIER,
    OFFER_TRUTH_TIER,
    derive_offer_id,
    derive_variant_sku_key,
    variant_own_price,
)
from services.catalog_offer_writer_guard import (  # noqa: E402
    WriterAuditAccumulator,
    guard_catalog_offer_rows,
    make_batch_id,
    write_writer_audit_log,
)
from services.catalog_variant_promoter import (  # noqa: E402
    _extract_variants_from_payload,
    _extract_variants_from_seed,
    filter_real_variants,
)
from services.variant_identity import (  # noqa: E402
    MERCHANT_ISSUED,
    variant_id_provenance,
)

logger = logging.getLogger("backfill_variant_identity_skus")

SOURCE_SYSTEM = "variant_identity_backfill_v1"
WRITER_NAME = "backfill_variant_identity_skus"

#: The contract token an operator must name to run this with --apply.
#:
#: WHY A SCRIPT NEEDS THIS. `scripts/ops/run_oneoff_job.sh` runs `backend:latest`, built from main.
#: The first draft of this file — the one with all four blockers — is ALREADY MERGED, so the run
#: command printed in a docstring executes whatever version the image happens to hold, which for
#: any window between merge and image rebuild is the broken one. Nothing about the command says
#: which version answered it. Measured 2026-09-07: the image's copy was byte-identical to the
#: broken draft while this fixed copy sat unmerged on a branch.
#:
#: `--apply` therefore requires `--expect-contract backfill-v2-identity-index`. The old draft has
#: no such flag, so argparse refuses it outright — a stale image fails loudly on the argument
#: instead of silently running the version that crashes on 30% of its plan. Bump the token whenever
#: a change would make an in-flight operator's command mean something different.
CONTRACT = "backfill-v2-identity-index"


def _is_unique_violation(exc: Exception) -> bool:
    """True only for a Postgres unique violation (SQLSTATE 23505).

    Same shape as `routes/billing_routes._is_unique_violation`: read the driver's sqlstate
    rather than importing asyncpg here, and fall back to the class name. Anything else —
    a missing arbiter index (42P10), a statement timeout, a truncation — must escape.
    """
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if code == "23505":
        return True
    return "uniqueviolation" in type(exc).__name__.lower()


class _OfferRefused(Exception):
    """The guard rejected this variant's offer, so its SKU must not be committed alone."""


class _OfferOwnedByOther(Exception):
    """This offer_id already exists and another writer owns it."""


class _SkuSuppressed(Exception):
    """The identity exists on a suppressed SKU; an offer there could never be served."""

#: The closed vocabulary catalog_offers.availability actually uses on this track. Raw crawl text
#: ("In Stock", "Out of Stock", "low stock") matches no reader in the repo, and the middle one is
#: the dangerous case: a sold-out variant that does not read as sold out.
_AVAILABILITY = {
    "in_stock": "in_stock", "instock": "in_stock", "in stock": "in_stock", "available": "in_stock",
    "out_of_stock": "out_of_stock", "outofstock": "out_of_stock", "out of stock": "out_of_stock",
    "sold_out": "out_of_stock", "sold out": "out_of_stock", "unavailable": "out_of_stock",
    "low_stock": "low_stock", "low stock": "low_stock", "limited": "low_stock",
    "unknown": "unknown",
}

#: Products are walked in a stable order so --limit / --after are reproducible and resumable.
#: The scan is paged because the one-off job runs under DB_STATEMENT_TIMEOUT_SECONDS=30 and a
#: single fetch_all of 13,799 rows with two jsonb blobs each does not finish inside it.
SELECT_PRODUCTS_SQL = """
    SELECT cp.product_key, cp.merchant_id, cp.platform, cp.source_product_id,
           cp.source_domain, cp.title,
           (eps.seed_data->'snapshot'->'variants') AS seed_variants,
           (cp.product_payload->'variants')        AS payload_variants
    FROM catalog_products cp
    LEFT JOIN external_product_seeds eps
      -- SCOPED (B3). The seed must claim THIS product and still be active; an id borrowed from
      -- another product is indistinguishable from a real one once it is a string.
      ON eps.external_product_id = cp.source_product_id
     AND eps.attached_product_key = cp.product_key
     AND eps.status = 'active'
    WHERE cp.platform = 'external_seed'
      AND cp.suppressed_at IS NULL
      -- BOTH columns: recall gates suppression_reason too, and catalog_trust_policy
      -- tombstones on the reason ALONE (a reason without a timestamp is a
      -- threshold-0 class in catalog_invariant_checks).
      AND cp.suppression_reason IS NULL
      AND cp.product_key > :after
    ORDER BY cp.product_key
    LIMIT :page
"""

#: LIVE offers only, and every field we clone comes from one of them. `suppressed_at IS NULL` is a
#: WHERE, never an ORDER BY — see note 2 in the module docstring.
SELECT_LIVE_OFFERS_SQL = """
    SELECT offer_id, merchant_id, catalog_track, truth_tier, readiness_tier,
           offer_mode, channel, availability, currency, source_domain, market,
           -- Decision-grade fields. Dropping them left every new offer reading
           -- "unknown seller / not official" beside a canonical sibling that reads
           -- brand_direct/official: pivot_query_service treats a NULL offer_type on
           -- external_referral as authoritative "unknown" and is_first_party false.
           offer_type, is_first_party, why_buy_direct,
           coalesce(offer_payload->>'destination_url', source_ref) AS destination_url
    FROM catalog_offers
    WHERE product_key = :pk
      AND suppressed_at IS NULL
    ORDER BY offer_id
"""

#: Conflict on the IDENTITY index, not the PK (B1). RETURNING hands back the sku_key that actually
#: holds this identity — which may be the promoter's spelling, already in the table.
UPSERT_SKU_SQL = """
    INSERT INTO catalog_skus (
        sku_key, product_key, merchant_id, platform, source_product_id,
        source_variant_id, source_domain, sku, barcode, title, currency,
        image_url, visible_attributes, visible_option_labels, ingredient_ids,
        sku_payload, readiness_tier, updated_at
    ) VALUES (
        :sku_key, :product_key, :merchant_id, :platform, :source_product_id,
        :source_variant_id, :source_domain, :sku, :barcode, :title, :currency,
        :image_url, CAST(:visible_attributes AS jsonb),
        CAST(:visible_option_labels AS jsonb), CAST(:ingredient_ids AS jsonb),
        CAST(:sku_payload AS jsonb), :readiness_tier, NOW()
    )
    -- WHERE on the DO UPDATE (F6): recall's candidate CTE filters
    -- `s.suppressed_at IS NULL AND s.suppression_reason IS NULL`, so attaching a live offer
    -- to a suppressed SKU creates supply nothing can ever surface. 0 of the 4,287 adopted
    -- rows are suppressed today. RETURNING then yields NULL, which the caller counts.
    ON CONFLICT (merchant_id, platform, product_key, source_variant_id) DO UPDATE SET
        -- MERGE, never replace: the existing blob may carry agent_version / source_handle /
        -- canonical_url from whoever wrote the row first, and destroying those would both lose
        -- provenance and mislabel their row as this batch's.
        -- coalesce because `NULL || jsonb` is NULL, which would silently ERASE the provenance
        -- marker this backfill exists to write. All 27,268 rows are objects today; the column
        -- is nullable, so the day one is not, the failure is invisible.
        sku_payload = coalesce(catalog_skus.sku_payload, '{}'::jsonb) || EXCLUDED.sku_payload,
        image_url   = coalesce(catalog_skus.image_url, EXCLUDED.image_url),
        barcode     = coalesce(catalog_skus.barcode, EXCLUDED.barcode),
        updated_at  = NOW()
    WHERE catalog_skus.suppressed_at IS NULL
      AND catalog_skus.suppression_reason IS NULL
    RETURNING sku_key
"""

UPSERT_OFFER_SQL = """
    INSERT INTO catalog_offers (
        offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
        readiness_tier, offer_mode, channel, availability, currency,
        list_price, merchant_effective_price, estimated_best_price,
        source_system, source_domain, market, source_ref,
        offer_type, is_first_party, why_buy_direct,
        offer_payload, created_at, updated_at
    ) VALUES (
        :offer_id, :sku_key, :product_key, :merchant_id, :catalog_track, :truth_tier,
        :readiness_tier, :offer_mode, :channel, :availability, :currency,
        :list_price, :merchant_effective_price, :estimated_best_price,
        :source_system, :source_domain, :market, :source_ref,
        :offer_type, :is_first_party, :why_buy_direct,
        CAST(:offer_payload AS jsonb), NOW(), NOW()
    )
    ON CONFLICT (offer_id) DO UPDATE SET
        list_price               = EXCLUDED.list_price,
        merchant_effective_price = EXCLUDED.merchant_effective_price,
        estimated_best_price     = EXCLUDED.estimated_best_price,
        -- Only when the VARIANT itself said something. _availability_of falls back to the
        -- chosen offer's value, so on an UPDATE a variant that carries no availability of its
        -- own would overwrite a measured out_of_stock with an inherited in_stock.
        availability             = CASE WHEN :variant_spoke
                                       THEN EXCLUDED.availability
                                       ELSE catalog_offers.availability END,
        -- Re-stamped on the update path too, so a row this batch last touched says so (M1).
        source_system            = EXCLUDED.source_system,
        offer_payload            = catalog_offers.offer_payload || EXCLUDED.offer_payload,
        updated_at               = NOW()
"""


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def _as_list(value: Any) -> List[Dict[str, Any]]:
    v = _jsonb(value)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _price_of(variant: Dict[str, Any]) -> Optional[float]:
    """The variant's OWN price. Never the product's — a variant differs from its siblings in
    exactly the dimension that carries price (30 ml vs 50 ml, a set vs a single).

    ONE RULE with ingestion, per #2116: `variant_own_price` is the single definition, so the
    ingest-time writer and this backfill cannot disagree about whether a row is priced. They
    did disagree once — the loop lifted by #2113 read `v.get("price")` directly and wrote NULL
    prices for variants that carry only `price_amount`."""
    return variant_own_price(variant)


def normalize_availability(raw: Any, fallback: Optional[str] = None) -> str:
    """Map crawl text onto the vocabulary readers actually match."""
    token = str(raw or "").strip().lower().replace("-", "_")
    if token in _AVAILABILITY:
        return _AVAILABILITY[token]
    if token:
        return "unknown"
    fb = str(fallback or "").strip().lower()
    return _AVAILABILITY.get(fb, "unknown")


def _variant_stated_availability(variant: Dict[str, Any]) -> bool:
    """Did this variant carry availability of its own, or are we inheriting the offer's?"""
    return variant.get("availability") not in (None, "") or "in_stock" in variant


def _availability_of(variant: Dict[str, Any], fallback: Optional[str]) -> str:
    if variant.get("availability") not in (None, ""):
        return normalize_availability(variant.get("availability"), fallback)
    if "in_stock" in variant:
        return "in_stock" if variant.get("in_stock") else "out_of_stock"
    return normalize_availability(None, fallback)


def _option_labels(variant: Dict[str, Any]) -> Tuple[List[str], Dict[str, str]]:
    labels: List[str] = []
    attrs: Dict[str, str] = {}
    options = variant.get("options")
    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict):
                continue
            axis = str(opt.get("axis_kind") or opt.get("name") or "").strip().lower()
            val = str(opt.get("value") or "").strip()
            if axis and val:
                attrs[axis] = val
                labels.append(f"{axis}_{val.lower().replace(' ', '_')}")
    elif isinstance(options, dict):
        for k, v in options.items():
            if isinstance(v, str) and v.strip():
                axis = str(k).strip().lower()
                attrs[axis] = v.strip()
                labels.append(f"{axis}_{v.strip().lower().replace(' ', '_')}")
    if not labels:
        shade = str(variant.get("title") or "").strip()
        if shade:
            attrs.setdefault("shade", shade)
            labels.append("shade_" + shade.lower().replace(" ", "_"))
    return labels, attrs


def choose_offer(
    live_offers: List[Dict[str, Any]],
    *,
    want_currency: str = "",
    source_domain: str = "",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The one live offer whose attribution every variant of this product will inherit.

    Refuses rather than guesses in the two cases where inheriting is a claim we cannot support:
    no live offer at all, and live offers from more than one merchant (729 such products on prod,
    max 3 merchants) — nothing about a variant makes it belong to whichever seller sorted first.
    """
    if not live_offers:
        return None, "no_live_offer"
    merchants = {str(o.get("merchant_id") or "") for o in live_offers}
    if len(merchants) > 1:
        return None, "multi_merchant_product"
    with_destination = [o for o in live_offers if str(o.get("destination_url") or "").strip()]
    if not with_destination:
        return None, "no_destination_url"
    if len({str(o.get("destination_url") or "") for o in with_destination}) == 1:
        return with_destination[0], None
    # 168 prod products have several live same-merchant offers with DIFFERENT destinations.
    # Taking [0] of an offer_id ordering makes the destination — and therefore offer_id,
    # market and the availability fallback — depend on which row happens to sort first, so a
    # new sibling offer changes the derived offer_id and a re-run writes a duplicate. That is
    # B4 again, one level down. Prefer the variant's own currency, then the product's own
    # domain, then the URL itself: all three are properties of the data, not of an ordering.
    def _rank(o):
        return (
            0 if want_currency and str(o.get("currency") or "").upper() == want_currency else 1,
            0 if source_domain and source_domain in str(o.get("destination_url") or "") else 1,
            str(o.get("destination_url") or ""),
        )
    return sorted(with_destination, key=_rank)[0], None


def plan_for_product(
    row: Dict[str, Any], counts: collections.Counter
) -> List[Dict[str, Any]]:
    product_key = row["product_key"]
    variants = _extract_variants_from_seed(
        {"snapshot": {"variants": _as_list(row["seed_variants"])}}
    ) or _extract_variants_from_payload({"variants": _as_list(row["payload_variants"])})
    out: List[Dict[str, Any]] = []
    for variant in filter_real_variants(variants):
        vid = str(variant.get("variant_id") or variant.get("id") or "").strip()
        if variant_id_provenance(
            vid, product_id=row["source_product_id"], product_key=product_key
        ) != MERCHANT_ISSUED:
            counts["skipped_not_merchant_issued"] += 1
            continue
        price = _price_of(variant)
        if price is None:
            counts["skipped_no_variant_price"] += 1
            continue
        out.append({"variant_id": vid, "variant": variant, "price": price})
    return out


async def run(
    *, apply: bool, limit: int = 0, after: str = "", page: int = 500,
    adopt_existing_offers: bool = False,
) -> Dict[str, Any]:
    counts: collections.Counter = collections.Counter()
    # Pre-seed every outcome so the report distinguishes "zero" from "never measured". A bare
    # Counter omits keys that never incremented, which makes a run that planned nothing look
    # like a run that did not check — and `record_info` drops <= 0 for the same reason, so the
    # same ambiguity reaches writer_audit_log.reasons.
    for _k in (
        "products_scanned", "products_planned", "skus", "offers",
        "skipped_not_merchant_issued", "skipped_no_variant_price",
        "skipped_no_live_offer", "skipped_multi_merchant_product",
        "skipped_no_destination_url", "skipped_currency_disagrees_with_offer",
        "skipped_offer_owned_by_other_writer", "skipped_suppressed_sku",
        "adopted_existing_sku_row", "rolled_back_offer_refused_by_guard",
        "skipped_unique_violation",
    ):
        counts[_k] = 0
    audit = WriterAuditAccumulator(
        writer_name=WRITER_NAME, batch_id=make_batch_id(SOURCE_SYSTEM)
    )
    cursor = after or ""
    touched = 0
    last_key = cursor

    state = {"last_key": last_key}
    try:
        return await _scan(
            counts, audit, apply=apply, limit=limit, page=page,
            cursor=cursor, touched=touched, state=state,
            adopt_existing_offers=adopt_existing_offers,
        )
    except Exception:
        # Seal the report and the audit row before the exception leaves, then let it leave.
        # Committed pairs are already durable; losing the cursor is what made recovery manual.
        await _finish(counts, audit, apply=apply, last_key=state["last_key"])
        raise


async def _scan(counts, audit, *, apply, limit, page, cursor, touched, state,
                adopt_existing_offers=False):
    while True:
        rows = await database.fetch_all(
            SELECT_PRODUCTS_SQL, {"after": cursor, "page": int(page)}
        )
        if not rows:
            break
        cursor = str(rows[-1]["product_key"])
        for row in rows:
            row = dict(row)
            counts["products_scanned"] += 1
            picks = plan_for_product(row, counts)
            if not picks:
                continue

            live = [dict(o) for o in await database.fetch_all(
                SELECT_LIVE_OFFERS_SQL, {"pk": row["product_key"]}
            ) or []]
            # The variant currencies present on this product, so the destination choice can
            # prefer an offer the variants actually agree with instead of an arbitrary one.
            _vcurs = {
                str(p["variant"].get("currency") or "").strip().upper() for p in picks
            } - {""}
            chosen, refusal = choose_offer(
                live,
                want_currency=next(iter(_vcurs)) if len(_vcurs) == 1 else "",
                source_domain=str(row.get("source_domain") or ""),
            )
            if chosen is None:
                counts["skipped_" + str(refusal)] += len(picks)
                continue

            offer_currency = str(chosen.get("currency") or "").strip().upper()
            planned: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            for pick in picks:
                vid, variant, price = pick["variant_id"], pick["variant"], pick["price"]
                vcur = str(variant.get("currency") or "").strip().upper()
                if vcur and offer_currency and vcur != offer_currency:
                    # The offer's market came from the chosen offer; a different currency means
                    # we do not know which market this price belongs to, and writing it anyway
                    # trips the ADR-024 market/currency invariant at offer grain.
                    counts["skipped_currency_disagrees_with_offer"] += 1
                    continue
                currency = vcur or offer_currency or "USD"
                labels, attrs = _option_labels(variant)
                sku_params = {
                    "sku_key": derive_variant_sku_key(row["product_key"], vid),
                    "product_key": row["product_key"],
                    "merchant_id": row["merchant_id"],
                    "platform": row["platform"],
                    "source_product_id": row["source_product_id"],
                    "source_variant_id": vid[:128],
                    "source_domain": row.get("source_domain"),
                    "sku": str(variant.get("sku") or "").strip() or None,
                    "barcode": str(variant.get("barcode") or "").strip() or None,
                    "title": str(variant.get("title") or "").strip() or row["title"],
                    "currency": currency,
                    "image_url": str(variant.get("image_url") or "").strip() or None,
                    "visible_attributes": json.dumps(attrs),
                    "visible_option_labels": json.dumps(labels),
                    "ingredient_ids": json.dumps([]),
                    "sku_payload": json.dumps({
                        "variant_id": vid,
                        "variant_id_provenance": MERCHANT_ISSUED,
                        "source_system": SOURCE_SYSTEM,
                        "batch_id": audit.batch_id,
                    }),
                    "readiness_tier": OFFER_READINESS_TIER,
                }
                planned.append((sku_params, {
                    "variant": variant, "price": price, "currency": currency,
                    "chosen": chosen,
                }))

            if not planned:
                continue
            counts["products_planned"] += 1

            for sku_params, meta in planned:
                if not apply:
                    counts["skus"] += 1
                    counts["offers"] += 1
                    continue
                # Counted AFTER the commit, not before. Incrementing here made a run whose
                # every pair rolled back report `skus: N, offers: 0` and an audit row saying
                # the same — indistinguishable from success to anyone reading the skus count.
                try:
                    async with database.transaction():
                        written_key = await database.fetch_val(UPSERT_SKU_SQL, sku_params)
                        if written_key is None:
                            # The DO UPDATE's WHERE refused it: the identity exists on a
                            # SUPPRESSED row. Nothing was written; do not invent a key.
                            raise _SkuSuppressed()
                        written_key = str(written_key)
                        # The identity already lived under another lane's spelling; we adopt
                        # that row rather than minting a rival for the same variant. Recorded
                        # only once the pair is COMMITTED — counting it here made a rolled-back
                        # pair still report itself as adopted.
                        adopted = written_key != sku_params["sku_key"]
                        chosen = meta["chosen"]
                        destination = str(chosen.get("destination_url") or "").strip()
                        offer_params = {
                            # Derived from the DESTINATION, which is a property of the product, not
                            # from whichever offer row an ORDER BY happened to return (B4).
                            "offer_id": derive_offer_id(
                                row["product_key"], written_key, destination
                            ),
                            "sku_key": written_key,
                            "product_key": row["product_key"],
                            "merchant_id": chosen.get("merchant_id") or row["merchant_id"],
                            "catalog_track": chosen.get("catalog_track") or OFFER_CATALOG_TRACK,
                            "truth_tier": chosen.get("truth_tier") or OFFER_TRUTH_TIER,
                            "readiness_tier": chosen.get("readiness_tier") or OFFER_READINESS_TIER,
                            "offer_mode": chosen.get("offer_mode") or OFFER_MODE,
                            "channel": chosen.get("channel") or "default",
                            "availability": _availability_of(
                                meta["variant"], chosen.get("availability")
                            ),
                            "variant_spoke": _variant_stated_availability(meta["variant"]),
                            "currency": meta["currency"],
                            "list_price": meta["price"],
                            "merchant_effective_price": meta["price"],
                            "estimated_best_price": meta["price"],
                            "source_system": SOURCE_SYSTEM,
                            "source_domain": row.get("source_domain") or chosen.get("source_domain"),
                            "market": chosen.get("market"),
                            # ingestion.py:1014 stores the destination here too, and
                            # SELECT_LIVE_OFFERS_SQL itself falls back to source_ref.
                            "source_ref": destination,
                            "offer_type": chosen.get("offer_type"),
                            "is_first_party": chosen.get("is_first_party"),
                            "why_buy_direct": chosen.get("why_buy_direct"),
                            "offer_payload": json.dumps({
                                "source_system": SOURCE_SYSTEM,
                                "batch_id": audit.batch_id,
                                "variant_id": sku_params["source_variant_id"],
                                "variant_id_provenance": MERCHANT_ISSUED,
                                "price_from": "variant",
                                # Carried, not merely claimed — without this every offer is a dead
                                # end and agent_shop_gateway filters it out.
                                "destination_url": destination,
                            }),
                        }
                        # derive_offer_id uses the same (product_key, sku_key, destination)
                        # triple ingestion does, so an id CAN land on a row another writer
                        # owns; DO UPDATE would then revert a price the nightly refresh had
                        # moved since the crawl and re-stamp source_system as ours. Measured
                        # 2026-09-07: 0 of 6,090 planned ids exist at all — but an ingest run
                        # between measurement and execution creates the case, so refuse by
                        # default rather than rely on a count that is only true today.
                        owner = await database.fetch_val(
                            "SELECT coalesce(source_system, '') FROM catalog_offers"
                            " WHERE offer_id = :oid",
                            {"oid": offer_params["offer_id"]},
                        )
                        if (
                            owner is not None
                            and str(owner) != SOURCE_SYSTEM
                            and not adopt_existing_offers
                        ):
                            raise _OfferOwnedByOther(str(owner))
                        accepted, reasons, _rejected = await guard_catalog_offer_rows(
                            [offer_params]
                        )
                        audit.record_skips(reasons)
                        for reason, n in reasons.items():
                            counts["guard_" + reason] += n
                        if not accepted:
                            # The SKU is already INSERTed in this transaction. Committing now
                            # would leave exactly the orphan SKU-without-offer state this
                            # backfill exists to remove — and silently, since only `counts`
                            # would record it. Roll the pair back instead: identity with no
                            # offer is not a partial success, it is the bug.
                            raise _OfferRefused()
                        await database.execute(UPSERT_OFFER_SQL, accepted[0])
                    # Outside the transaction block: both rows are committed now.
                    counts["skus"] += 1
                    counts["offers"] += 1
                    if adopted:
                        counts["adopted_existing_sku_row"] += 1
                    # One SKU + one offer, both committed. Charged here rather than beside the
                    # offer insert, where it counted 2 per accepted offer and 0 for a SKU whose
                    # offer was rejected — so applied_rows read 2 x offers, not skus + offers.
                    audit.record_applied(2)
                except _OfferRefused:
                    counts["rolled_back_offer_refused_by_guard"] += 1
                except _SkuSuppressed:
                    counts["skipped_suppressed_sku"] += 1
                except _OfferOwnedByOther as owned:
                    counts["skipped_offer_owned_by_other_writer"] += 1
                    counts["owner_" + (str(owned) or "unknown")] += 1
                except Exception as exc:  # noqa: BLE001
                    # The one path ON CONFLICT still cannot cover: the supplied sku_key
                    # collides with the PK while the identity tuple does NOT match, so
                    # neither conflict target applies. 0 of 6,090 rows today, but it becomes
                    # reachable when two variant ids normalize to the same 60-char token
                    # (_normalize_token lowercases and collapses punctuation, so ABC_1 and
                    # abc-1 collide) or when a product's merchant_id is re-resolved. Counting
                    # and continuing beats killing a 6,090-row run on one row.
                    #
                    # MATCH THE SQLSTATE, NOT THE MESSAGE. A first version tested
                    # `"unique" in repr(exc)`, which also swallows SQLSTATE 42P10 —
                    # "there is no UNIQUE or exclusion constraint matching the ON CONFLICT
                    # specification". That is the arbiter index being absent or renamed, i.e.
                    # precisely the B1 failure this script exists to avoid, and it would have
                    # been reported as row-level collisions while writing nothing and exiting
                    # 0. Proven by dropping idx_catalog_skus_source_identity_v2 and running.
                    if not _is_unique_violation(exc):
                        raise
                    counts["skipped_unique_violation"] += 1
                    logger.warning(
                        "unique violation on %s: %s", sku_params["sku_key"], repr(exc)[:200]
                    )

            state["last_key"] = row["product_key"]
            touched += 1
            if limit and touched >= limit:
                counts["stopped_at_limit"] = 1
                break
        if limit and touched >= limit:
            break

    return await _finish(counts, audit, apply=apply, last_key=state["last_key"])


async def _finish(counts, audit, *, apply: bool, last_key: str) -> Dict[str, Any]:
    """Seal the report. Called on the way out however the run ends.

    Without this, an exception mid-scan lost the report, the resume cursor AND the audit row
    while committed pairs stayed in the table — recoverable only by hand, from
    `SELECT max(product_key) FROM catalog_offers WHERE source_system = ...`.
    """
    counts["applied"] = 1 if apply else 0
    counts["resume_after"] = last_key
    if apply:
        # record_info drops <= 0, so a zero counter never reaches writer_audit_log.reasons
        # even though it is in the JSON report. Send the zeros under an explicit key instead
        # of leaving "measured zero" and "never measured" indistinguishable there.
        ints = {k: v for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool)}
        audit.record_info({k: v for k, v in ints.items() if v > 0})
        audit.reasons["zero_counters"] = sorted(k for k, v in ints.items() if v == 0)
        await write_writer_audit_log(audit)
        counts["batch_id"] = audit.batch_id
    return dict(counts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill variant SKU+offer pairs.")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--limit", type=int, default=0, help="stop after N products (0 = all)")
    ap.add_argument("--after", default="", help="resume: only product_key > this")
    ap.add_argument("--page", type=int, default=500, help="scan page size")
    ap.add_argument(
        "--adopt-existing-offers",
        action="store_true",
        help="overwrite an offer another writer owns. Off by default: DO UPDATE would revert "
             "a price the nightly refresh moved since the crawl and re-stamp its source_system.",
    )
    ap.add_argument(
        "--expect-contract",
        default="",
        help=f"required with --apply; must be {CONTRACT!r}. Its purpose is to fail on a stale "
             "image: the merged first draft has no such flag, so argparse rejects the command "
             "rather than running the broken version.",
    )
    args = ap.parse_args()
    if args.apply and args.expect_contract != CONTRACT:
        ap.error(
            f"--apply requires --expect-contract {CONTRACT}. Got {args.expect_contract!r}. "
            "If you passed the right token and still see this, the image is running a "
            "different version of this script than the one you read."
        )
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _go():
        await database.connect()
        try:
            return await run(
                apply=args.apply, limit=args.limit, after=args.after, page=args.page,
                adopt_existing_offers=args.adopt_existing_offers,
            )
        finally:
            await database.disconnect()

    print(json.dumps(asyncio.run(_go()), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
