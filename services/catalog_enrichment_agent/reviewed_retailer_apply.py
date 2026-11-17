"""Apply the exact reviewed retailer cohort inside the caller's stage transaction."""

import json
import os
from datetime import datetime

from services.catalog_enrichment_agent import apply as enrichment
from services.catalog_enrichment_agent.reviewed_retailer_offers import scope_reviewed_plan
from services.catalog_identity import validated_source_gtin
from services.exact_retailer_cohorts import (
    COHORT_A_CONFIG, COHORT_B_CONFIG, COHORT_C_REVIEW_LABEL,
    COHORT_B_BARE_FAMILY_URL, COHORT_B_EXPANSION_FLAG, COHORT_B_INITIAL_OFFER_IDS, RETAILER_COHORT_B_INITIAL,
    COHORT_B_PROTECTED_FAMILY_URLS, COHORT_B_PROTECTED_LISTING_KEYS, RETAILER_COHORT_B, PROOF_KEY,
    RETAILER_COHORT_A, RETAILER_COHORT_C,
)
from services.exact_retailer_shades import require_canonical_shades


# Distinct from cohort B expansion (…, 2) and from the private bundles' session lock (…, 3).
COHORT_C_LOCK = (1160308171, 5)


async def require_open_serializable_transaction(db) -> None:
    """Refuse unless the caller holds an explicit serializable transaction.

    SHOW transaction_isolation alone also reads 'serializable' from a session default in
    autocommit mode. Two statements report the same assigned transaction id only when they run
    inside one transaction block; in autocommit each statement is its own transaction, so the
    second reads NULL.
    """
    if await db.fetch_val("SHOW transaction_isolation") != "serializable":
        raise ValueError("cohort C writes require a serializable transaction")
    assigned = await db.fetch_val("SELECT txid_current()::text")
    if not assigned or await db.fetch_val("SELECT txid_current_if_assigned()::text") != assigned:
        raise ValueError("cohort C writes require an open transaction")


async def apply_reviewed_retailer(db, record, *, write: bool, cohort=RETAILER_COHORT_A, expand_cohort_b=False):
    """Confirm identity and ownership within the caller's transaction before writing."""
    if cohort is None:
        raise ValueError("private retailer manifest is required")
    is_cohort_a = cohort is RETAILER_COHORT_A
    is_cohort_b = cohort is RETAILER_COHORT_B or cohort is RETAILER_COHORT_B_INITIAL
    is_cohort_c = cohort is RETAILER_COHORT_C
    if expand_cohort_b:
        if cohort is not RETAILER_COHORT_B:
            raise ValueError("the exact cohort B expansion cohort is required")
        if write:
            if os.getenv(COHORT_B_EXPANSION_FLAG) != "1" or await db.fetch_val("SHOW transaction_isolation") != "serializable":
                raise ValueError("cohort B expansion requires its opt-in and serializable transaction")
            await db.execute("SELECT pg_advisory_xact_lock(1160308171, 2)")
    if is_cohort_c and write:
        await require_open_serializable_transaction(db)
        await db.execute(f"SELECT pg_advisory_xact_lock({COHORT_C_LOCK[0]}, {COHORT_C_LOCK[1]})")
    label = (COHORT_A_CONFIG if is_cohort_a else COHORT_B_CONFIG)["review_label"]
    if is_cohort_c:
        label = COHORT_C_REVIEW_LABEL
    scope = {"include_reviewed_cohort_a": True} if is_cohort_a else {"reviewed_retailer_keys": tuple(cohort.listings)}
    plan = scope_reviewed_plan(
        await enrichment.plan_with_current_listings(
            [record], db=db, market="US", source_jsonl=label, **scope,
        ),
        cohort=cohort,
    )
    if plan["listing_spellings"]["status"] != "read":
        raise ValueError("stored retailer listing spellings were not verified")
    from services.intake_identity import (
        DOOR_CATALOG_ENRICHMENT,
        intake_identity_enabled,
        variant_barcode_match_enabled,
    )

    if not (intake_identity_enabled(DOOR_CATALOG_ENRICHMENT) and variant_barcode_match_enabled()):
        raise ValueError("existing exact identity gate is disabled")
    product = plan["pdps"][0]
    key = product["product_key"]
    retailer = cohort.listings[key]
    params = {"key": key, "seller": retailer.merchant_id, "host": retailer.host, "url": retailer.family_url}
    lock = " FOR UPDATE" if write else ""
    observed = await db.fetch_one(
        """
        SELECT to_jsonb(m)::text AS row_json
        FROM catalog_merchants m
        WHERE m.merchant_id = :seller AND m.status = 'observed' AND m.source_ref = :host
          AND m.metadata_json->'seller_identity'->>'kind' = 'retailer'
          AND m.metadata_json->'seller_identity'->>'etld1' = :host
        """ + lock,
        {"seller": retailer.merchant_id, "host": retailer.host},
    )
    if not observed:
        raise ValueError("reviewed observed retailer is missing")
    if is_cohort_c:
        marked = await db.fetch_all(
            """
            SELECT merchant_id FROM catalog_merchants m
            WHERE m.metadata_json->'seller_identity'->>'kind' = 'retailer'
              AND m.metadata_json->'seller_identity'->>'etld1' = :host
            """,
            {"host": retailer.host},
        )
        if [row["merchant_id"] for row in marked] != [retailer.merchant_id]:
            raise ValueError("exactly one retailer-marked merchant record must own the host")
    if await db.fetch_val(
        """
        SELECT EXISTS (SELECT 1 FROM brand_claims
          WHERE verification_status='verified'
            AND (lower(brand_domain)=:host OR lower(brand_domain) LIKE '%.' || :host))
        """, {"host": retailer.host}
    ):
        raise ValueError("retailer has a verified domain claim")
    canonical = await canonical_witness(db, retailer, cohort=cohort)
    aliases = {COHORT_B_BARE_FAMILY_URL.removeprefix("https://"): key} if is_cohort_b else None
    conflicts = await enrichment.find_legacy_retailer_listing_owners(plan, db, listing_aliases=aliases)
    if any(row["kind"] != "retired_owner" for row in conflicts):
        raise ValueError("a competing active listing already owns the retailer URL")
    protected_urls = COHORT_B_PROTECTED_FAMILY_URLS if is_cohort_b else (retailer.family_url,)
    protected_keys = sorted(COHORT_B_PROTECTED_LISTING_KEYS) if is_cohort_b else [key]
    if await db.fetch_val(
        """
        SELECT EXISTS (SELECT 1 FROM external_product_seeds
         WHERE coalesce(nullif(lower(trim(status)),''),'active')='active'
           AND (attached_product_key=ANY(:keys)
             OR lower(rtrim(regexp_replace(split_part(split_part(canonical_url,'?',1),'#',1),
                                           '^https?://(www[.])?', '', 'i'),'/'))=ANY(:listings)
             OR lower(rtrim(regexp_replace(split_part(split_part(destination_url,'?',1),'#',1),
                                           '^https?://(www[.])?', '', 'i'),'/'))=ANY(:listings)))
        """, {"keys": protected_keys, "listings": [url.removeprefix("https://") for url in protected_urls]}
    ):
        raise ValueError("an active retailer source seed already owns the listing")
    listing = await db.fetch_one(
        """
        SELECT merchant_id, canonical_url, content_key, suppressed_at, suppression_reason, pivota_signature_id
        FROM catalog_products WHERE product_key=:key
        """ + lock, {"key": key}
    )
    if listing and dict(listing) != {
        "merchant_id": retailer.merchant_id, "canonical_url": retailer.family_url,
        "content_key": cohort.canonical_content_key, "suppressed_at": None, "suppression_reason": None,
        "pivota_signature_id": None,
    }:
        raise ValueError("retailer product key is already occupied by another identity")

    expected = {offer["offer_id"]: json.loads(offer["offer_payload"])[PROOF_KEY] for offer in plan["offers"]}
    before = await retailer_witness(db, params)
    incoming = {datetime.fromisoformat(proof["observed_at"]) for proof in expected.values()}
    if len(incoming) != 1:
        raise ValueError("reviewed retailer offers have different capture times")
    existing_ids = {row["offer_id"] for row in before}
    if expand_cohort_b:
        if len(expected) != 6 or not COHORT_B_INITIAL_OFFER_IDS <= expected.keys() or (
            existing_ids != COHORT_B_INITIAL_OFFER_IDS and existing_ids != expected.keys()
        ):
            raise ValueError("cohort B expansion requires exactly the prior two or the complete six offers")
        if await db.fetch_val("""
            SELECT EXISTS (SELECT 1 FROM catalog_offers
              WHERE product_key=:key AND market IS DISTINCT FROM 'US')
        """, {"key": key}):
            raise ValueError("cohort B expansion found another market on the reviewed listing")
    elif cohort is RETAILER_COHORT_B and not before:
        raise ValueError("the six cohort B offers require the explicit reviewed expansion")

    replay = False
    if before:
        prior_scope = {offer_id: expected[offer_id] for offer_id in existing_ids} if expand_cohort_b else expected
        stored = _reviewed_observation_times(before, prior_scope, params, cohort)
        already_expanded = expand_cohort_b and existing_ids == expected.keys()
        if not already_expanded and min(incoming) < max(stored):
            raise ValueError("retailer capture predates the persisted observation")
        if expand_cohort_b:
            # The first expansion adds four offers without rewriting either original quote or SKU.
            replay = already_expanded
            persisted = {row["offer_id"]: _row_proof(row) for row in before}
            require_offer_snapshot(before, _proof_offer_snapshot(persisted))
            expected.update(persisted)
        elif incoming == stored:
            replay = True
            require_retailer_rows(before, expected, params, cohort=cohort)
            require_offer_snapshot(before, _proof_offer_snapshot(expected))
    receipt = {"product_key": key, "seller_ref": retailer.merchant_id, "offers": expected,
               "existing": len(before), "replayed": replay}
    if not write or replay:
        return receipt

    if expand_cohort_b:
        offers = [offer for offer in plan["offers"] if offer["offer_id"] not in existing_ids]
        sku_keys = {offer["sku_key"] for offer in offers}
        plan = {**plan, "offers": offers, "skus": [sku for sku in plan["skus"] if sku["sku_key"] in sku_keys]}
    expected_writes = 4 if expand_cohort_b else len(retailer.variants)
    counts = await enrichment.apply_ingest_plan(
        plan, batch_label=label, db=db, market="US", price_read=True,
    )
    if {k: counts.get(k) for k in ("pdps", "skus", "offers", "seeds")} != {
        "pdps": 1, "skus": expected_writes, "offers": expected_writes, "seeds": 0,
    } or any(counts.get(k) for k in ("product_groups_failed", "offers_skipped", "skus_identity_conflict")):
        raise ValueError("retailer write was incomplete")
    after = await retailer_witness(db, params)
    require_retailer_rows(after, expected, params, cohort=cohort)
    require_offer_snapshot(after, _proof_offer_snapshot(expected))
    if expand_cohort_b and {r["offer_id"]: r for r in after if r["offer_id"] in existing_ids} != {
        r["offer_id"]: r for r in before
    }:
        raise ValueError("cohort B expansion changed an original SKU or quote")
    if await canonical_witness(db, retailer, cohort=cohort) != canonical:
        raise ValueError("reviewed canonical product or index changed during retailer apply")
    if await db.fetch_val(
        "SELECT to_jsonb(m)::text FROM catalog_merchants m WHERE m.merchant_id=:seller",
        {"seller": retailer.merchant_id},
    ) != observed["row_json"]:
        raise ValueError("existing seller row changed during retailer apply")
    return receipt


async def canonical_witness(db, retailer, *, cohort=RETAILER_COHORT_A):
    rows = await db.fetch_all(
        """
        SELECT to_jsonb(p)::text AS product_row, to_jsonb(i)::text AS index_row,
          to_jsonb(a)::text AS pdp_row, to_jsonb(s)::text AS sku_row, s.source_variant_id, s.barcode,
          (SELECT jsonb_agg(to_jsonb(o) ORDER BY o.offer_id)::text
           FROM catalog_offers o WHERE o.sku_key=s.sku_key AND o.merchant_id=p.merchant_id) AS offer_rows
        FROM catalog_products p
        JOIN index_pipeline_state i ON i.content_key=p.content_key AND i.serving_eligible
        JOIN agent_pdp_view a ON a.content_key=p.content_key
        JOIN product_group_members g ON g.merchant_id=p.merchant_id AND g.platform=p.platform
          AND g.platform_product_id=p.source_product_id AND g.is_primary
        JOIN catalog_skus s ON s.product_key=p.product_key AND s.merchant_id=p.merchant_id
        WHERE p.product_key=:key AND p.merchant_id=:seller AND p.content_key=:content
          AND g.product_group_id=:group_id AND p.sync_status='live'
          AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
          AND s.source_variant_id=ANY(:variants)
        ORDER BY s.source_variant_id
        """, {"key": cohort.canonical_product_key, "seller": cohort.canonical_merchant_id,
              "content": cohort.canonical_content_key, "group_id": cohort.canonical_group_id,
              "variants": sorted(v[0] for v in retailer.variants.values())}
    )
    observed = {(row["source_variant_id"], validated_source_gtin(row["barcode"])) for row in rows}
    if len(rows) != len(retailer.variants) or observed != set(retailer.variants.values()):
        raise ValueError("canonical product, index or exact UPC identities are missing")
    if cohort.canonical_signature_id is not None:
        await require_canonical_shades(db, cohort=cohort)
    return [dict(row) for row in rows]


async def retailer_witness(db, params):
    return [dict(row) for row in await db.fetch_all(
        """
        SELECT o.offer_id, o.merchant_id, o.source_ref, o.market, o.currency, o.offer_type, o.offer_mode,
          o.suppressed_at, o.suppression_reason, o.availability, o.list_price,
          o.offer_payload->'exact_retailer_shade_v1' AS proof,
          s.merchant_id AS sku_seller, s.source_variant_id, s.barcode, p.merchant_id AS product_seller,
          to_jsonb(o)::text AS offer_row, to_jsonb(s)::text AS sku_row,
          p.content_key, g.product_group_id, p.pivota_signature_id
        FROM catalog_offers o
        LEFT JOIN catalog_skus s ON s.sku_key=o.sku_key AND s.product_key=o.product_key
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
        LEFT JOIN catalog_products p ON p.product_key=o.product_key AND p.suppressed_at IS NULL
          AND p.suppression_reason IS NULL AND p.canonical_url=:url
        LEFT JOIN product_group_members g ON g.merchant_id=p.merchant_id
          AND g.platform=p.platform AND g.platform_product_id=p.source_product_id AND NOT g.is_primary
        WHERE o.product_key=:key AND o.market='US'
        ORDER BY o.offer_id
        """, {"key": params["key"], "url": params["url"]}
    )]


def require_offer_snapshot(rows, expected_money):
    actual = {row["offer_id"]: (row["availability"], float(row["list_price"])) for row in rows}
    if actual != expected_money:
        raise ValueError("retailer availability or price differs from the observed snapshot")


def _row_proof(row):
    proof = row["proof"]
    return json.loads(proof) if isinstance(proof, str) else proof


def _proof_offer_snapshot(proofs):
    return {offer_id: (proof["availability"], proof["price_minor"] / 100) for offer_id, proof in proofs.items()}


def _reviewed_observation_times(rows, expected, params, cohort):
    observed = require_retailer_rows(rows, expected, params, compare_snapshot=False, cohort=cohort)
    if len(observed) == 1:
        return observed
    # The first six-offer state may have one previous timestamp for the two original quotes
    # and one for the four additions. The normal refresh replaces both with a single capture.
    if cohort is RETAILER_COHORT_B and len(expected) == 6 and COHORT_B_INITIAL_OFFER_IDS <= expected.keys():
        original, added = set(), set()
        for row in rows:
            timestamps = original if row["offer_id"] in COHORT_B_INITIAL_OFFER_IDS else added
            timestamps.add(datetime.fromisoformat(_row_proof(row)["observed_at"]))
        if len(original) == len(added) == 1 and max(original) <= min(added):
            return observed
    raise ValueError("retailer persisted observations are inconsistent")


def require_retailer_rows(rows, expected, params, *, compare_snapshot=True, cohort=RETAILER_COHORT_A):
    if len(rows) != len(expected) or {row["offer_id"] for row in rows} != expected.keys():
        raise ValueError("retailer offer IDs are incomplete or collide with an earlier writer")
    observed_times = set()
    for row in rows:
        proof = _row_proof(row)
        wanted = expected[row["offer_id"]]
        if not isinstance(proof, dict) or not isinstance(proof.get("observed_at"), str):
            raise ValueError("retailer offer lacks source capture time")
        observed_time = datetime.fromisoformat(proof["observed_at"])
        if observed_time.tzinfo is None:
            raise ValueError("retailer capture time lacks a timezone")
        observed_times.add(observed_time)
        comparable = proof if compare_snapshot else {
            **proof, **{field: wanted[field] for field in ("observed_at", "price_minor", "availability")}
        }
        if (
            comparable != wanted
            or (row["merchant_id"], row["sku_seller"], row["product_seller"]) != (params["seller"],) * 3
            or (row["source_ref"], row["market"], row["currency"], row["offer_type"], row["offer_mode"])
            != (params["url"], "US", "USD", "retailer", "redirect")
            or (row["content_key"], row["product_group_id"])
            != (cohort.canonical_content_key, cohort.canonical_group_id)
            or row["pivota_signature_id"] is not None
            or row["suppression_reason"] is not None or row["suppressed_at"] is not None
            or row["source_variant_id"] != wanted["retailer_native_variant_id"]
            or validated_source_gtin(row["barcode"]) != wanted["gtin14"]
        ):
            raise ValueError("retailer offer owner, native shade, group or refresh anchor changed")
    return observed_times
