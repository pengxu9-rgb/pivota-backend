"""Project four independently reviewed AMUSE shades onto the existing US seed."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os

from db.database import database
from routes.employee_products import _seed_review_version
from services.catalog_enrichment_agent.apply import plan_with_current_listings
from services.catalog_enrichment_agent.surratt_retailer_offers import scope_reviewed_plan
from services.catalog_identity import validated_source_gtin
from services.catalog_variant_promoter import promote_variants_for_group
from services.external_seed_model_review import ReviewTarget
from services.external_seed_review_pilot import (
    AMUSE_SEED_ID, EVIDENCE_MAX_AGE, LISTINGS, adjudicate, capture_shopify_evidence,
)
from services.seed_data_writer import refresh_agent_pdp_view_for_seed, upsert_seed_data
from services.surratt_retailer_pilot import KIOKII_RETAILER_COHORT, PROOF_KEY


EXTERNAL_ID = "ext_51577f22556c1f3aeb091391"
ORIGINAL = LISTINGS[AMUSE_SEED_ID]
ADDITIONS = (
    ("52547428909370", "8800299133487", "01 ZEN MATCHA", "8800299133487"),
    ("52547428974906", "8800299133500", "03 MANUCA HONEY", "8800299133500"),
    ("52547429040442", "8800299133524", "05 CRANBERRY POP", "8800299133524"),
    ("52547429073210", "8800299133531", "06 ROSE SORBET", "8800299133531"),
)
POLICY_VERSION = "amuse-us-variant-expansion-v1"
REVIEW_LISTING = replace(ORIGINAL, variants=ADDITIONS, policy_version=POLICY_VERSION,
                         native_shop_domain="amuse-global.myshopify.com", machine_activation_allowed=False)
INITIAL_VARIANTS = {native: (gtin, title, sku) for native, gtin, title, sku in ORIGINAL.variants}
EXPANDED_VARIANTS = {native: (gtin, title, sku) for native, gtin, title, sku in ORIGINAL.variants + ADDITIONS}
COHORT = KIOKII_RETAILER_COHORT


@dataclass(frozen=True)
class ReviewedAnchors:
    version: str
    captured_at: datetime
    variants: tuple[dict, ...]
    evidence_digest: str
    model_receipts: dict
    retailer_quotes: dict


def seed_variants(row):
    if row is None or tuple(row[key] for key in (
        "id", "external_product_id", "seller_ref", "market", "tool", "status", "seed_kind",
        "domain", "destination_url", "canonical_url", "attached_product_key",
    )) != (
        AMUSE_SEED_ID, EXTERNAL_ID, ORIGINAL.seller_ref, "US", "*", "active", "cross",
        ORIGINAL.host, ORIGINAL.url, ORIGINAL.url, COHORT.canonical_product_key,
    ):
        raise ValueError("expected the existing active AMUSE US seed and Commerce Index identity")
    data = row["seed_data"]
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict) or str(data.get("product_id")) != str(ORIGINAL.native_product_id):
        raise ValueError("AMUSE official native product identity changed")
    current = data.get("variants")
    if not isinstance(current, list) or any(not isinstance(variant, dict) for variant in current):
        raise ValueError("AMUSE seed variants are unavailable")
    by_id = {str(variant.get("variant_id")): variant for variant in current}
    if len(by_id) != len(current) or by_id.keys() not in (INITIAL_VARIANTS.keys(), EXPANDED_VARIANTS.keys()):
        raise ValueError("AMUSE seed has a partial, duplicate, or unexpected shade set")
    for native, variant in by_id.items():
        gtin, title, sku = EXPANDED_VARIANTS[native]
        if (validated_source_gtin(variant.get("barcode")), variant.get("sku")) != (gtin.zfill(14), sku):
            raise ValueError("AMUSE seed SKU or GTIN changed")
        if str(variant.get("title") or "").strip().casefold() != title.casefold():
            raise ValueError("AMUSE seed shade title changed")
    return data, by_id


async def load_seed(*, lock=False):
    sql = "SELECT * FROM external_product_seeds WHERE id=:seed"
    row = await database.fetch_one(sql + (" FOR UPDATE" if lock else ""), {"seed": AMUSE_SEED_ID})
    return dict(row) if row else None


def materialize_anchors(evidence):
    """The official merchant SKU and a retailer barcode bind each original shade."""
    sources = evidence["sources"]
    (retailer,) = COHORT.listings.values()
    new = []
    for native, gtin, title, sku in ADDITIONS:
        official = sources[f"shopify_us:{native}"]
        corroboration = sources[f"barcode:{native}"]
        retailer_id = corroboration["variant_id"]
        landing = f"{ORIGINAL.url}?variant={native}"
        if retailer.variants.get(retailer_id) != (native, gtin.zfill(14)) or (
            official["variant_id"], official["merchant_sku"], official["offer_url"],
            official["currency"], official["country"], official["in_stock"], official["shade"],
            corroboration["native_barcode"], official["price_minor"],
        ) != (native, sku, landing, "USD", "US", True, title, gtin, ORIGINAL.price_minor):
            raise ValueError("AMUSE exact official and Kiokii source identities disagree")
        new.append({
            "variant_id": native, "title": title, "sku": sku, "barcode": gtin,
            "option_name": "SHADE", "option_value": title, "url": landing,
            "price_amount": ORIGINAL.price_minor / 100, "price_currency": "USD",
            "availability": "in_stock", "in_stock": True,
            "identity_evidence": {
                "official_sku": sku, "official_native_barcode": official["native_barcode"],
                "retailer_barcode_url": corroboration["source_url"], "retailer_native_variant_id": retailer_id,
                "observed_at": official["observed_at"],
            },
        })
    return tuple(new)


async def capture_retailer_targets(evidence, record):
    listing = next(iter(COHORT.listings.values()))
    plan = scope_reviewed_plan(
        await plan_with_current_listings(
            [record], db=database, market="US", source_jsonl=POLICY_VERSION,
            reviewed_retailer_keys=tuple(COHORT.listings),
        ), cohort=COHORT,
    )
    if plan["listing_spellings"]["status"] != "read":
        raise ValueError("Kiokii listing identity could not be resolved before review")
    targets, quotes = [], {}
    for offer in plan["offers"]:
        quote = json.loads(offer["offer_payload"])[PROOF_KEY]
        native = quote["canonical_native_variant_id"]
        if native in INITIAL_VARIANTS:
            continue
        if native not in EXPANDED_VARIANTS or quote["availability"] != "in_stock":
            raise ValueError("new reviewed Kiokii shade is unavailable or outside scope")
        offer_id = offer["offer_id"]
        ref = f"kiokii_us:{quote['retailer_native_variant_id']}"
        evidence["sources"][ref] = {
            "variant_id": quote["retailer_native_variant_id"], "gtin": quote["gtin14"],
            "shade": EXPANDED_VARIANTS[native][1], "price_minor": quote["price_minor"],
            "in_stock": True, "country": "US", "currency": "USD",
            "observed_at": quote["observed_at"], "native_barcode": quote["gtin14"],
            "gtin_provenance": "native_barcode",
            "offer_url": quote["retailer_landing_url"], "source_url": listing.family_url + ".js?country=US",
            "shipping_source_url": f"https://{listing.host}/en-us/meta.json",
            "shipping_scope": "store_declares_us_destination", "currency_proof": "product_cart_currency_cookie",
        }
        target = ReviewTarget(native, "exact_retailer_offer", offer_id)
        targets.append(target)
        evidence["target_sources"].append({
            "variant_id": native, "scope": target.scope, "offer_id": offer_id,
            "evidence_refs": [ref, f"shopify_us:{native}", f"barcode:{native}"],
        })
        quotes[offer_id] = quote
    if {target.variant_id for target in targets} != EXPANDED_VARIANTS.keys() - INITIAL_VARIANTS.keys():
        raise ValueError("the review packet does not cover all four distinct new Kiokii offers")
    return tuple(targets), quotes


async def prepare_review(record):
    row = await load_seed()
    seed_variants(row)
    version = _seed_review_version(row)
    evidence = await capture_shopify_evidence(REVIEW_LISTING)
    if not evidence.get("sources"):
        raise ValueError(f"AMUSE official variant capture held: {evidence.get('reason', 'no_sources')}")
    prospective = materialize_anchors(evidence)
    targets, quotes = await capture_retailer_targets(evidence, record)
    result = await adjudicate(AMUSE_SEED_ID, version, REVIEW_LISTING, evidence, exact_retailer_targets=targets)
    if not result.get("eligible"):
        review_state = (result.get("result") or {}).get("state")
        raise ValueError(f"AMUSE official variant machine review held: {review_state}")
    return ReviewedAnchors(version, datetime.fromisoformat(evidence["captured_at"]), prospective,
                           result["evidence_digest"], result["receipts"], quotes)


async def canonical_rows():
    return [dict(row) for row in await database.fetch_all(
        """
        SELECT s.sku_key, s.source_variant_id, s.sku, s.barcode, s.title,
               p.pivota_signature_id, p.content_key, g.product_group_id,
               to_jsonb(s)::text AS sku_snapshot,
               ARRAY(SELECT to_jsonb(o)::text FROM catalog_offers o WHERE o.sku_key=s.sku_key
                 AND o.merchant_id=p.merchant_id ORDER BY o.offer_id) AS offer_snapshot,
               ARRAY(SELECT o.offer_id FROM catalog_offers o WHERE o.sku_key=s.sku_key
                 AND o.merchant_id=p.merchant_id AND o.market='US' AND o.suppressed_at IS NULL
                 AND o.suppression_reason IS NULL ORDER BY o.offer_id) AS us_offers
        FROM catalog_products p
        JOIN index_pipeline_state i ON i.content_key=p.content_key AND i.serving_eligible
        JOIN agent_pdp_view a ON a.content_key=p.content_key
        JOIN product_group_members g ON g.merchant_id=p.merchant_id AND g.platform=p.platform
          AND g.platform_product_id=p.source_product_id AND g.is_primary
        JOIN catalog_skus s ON s.product_key=p.product_key AND s.merchant_id=p.merchant_id
        WHERE p.product_key=:product AND p.merchant_id=:seller AND g.product_group_id=:group_id
          AND p.content_key=:content AND p.pivota_signature_id='sig_f574469185cec88071f23f7080adaeb2'
          AND p.sync_status='live' AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
          AND s.source_variant_id=ANY(:variants)
        ORDER BY s.source_variant_id
        """, {"product": COHORT.canonical_product_key, "seller": COHORT.canonical_merchant_id,
              "group_id": COHORT.canonical_group_id, "content": COHORT.canonical_content_key,
              "variants": list(EXPANDED_VARIANTS)},
    )]


def verify_projection(rows, expected):
    actual = {row["source_variant_id"]: row for row in rows}
    if len(rows) != len(actual) or actual.keys() != expected.keys():
        raise ValueError("Commerce Index did not return precisely the expected AMUSE shade anchors")
    for native, row in actual.items():
        barcode, _, sku = expected[native]
        if (validated_source_gtin(row["barcode"]), row["sku"]) != (barcode.zfill(14), sku) or not row["us_offers"]:
            raise ValueError("Commerce Index AMUSE anchor lacks an exact GTIN or US offer")
    return actual


def projection_summary(rows):
    return [{key: item for key, item in row.items() if key not in ("sku_snapshot", "offer_snapshot")}
            for row in rows.values()]


async def assert_unique_additions():
    barcodes = sorted({gtin.zfill(width) for _, gtin, _, _ in ADDITIONS for width in (13, 14)})
    if await database.fetch_val(
        """
        SELECT EXISTS (
          SELECT 1 FROM catalog_skus s JOIN catalog_products p ON p.product_key=s.product_key
          JOIN index_pipeline_state i ON i.content_key=p.content_key AND i.serving_eligible
          JOIN product_group_members g ON g.merchant_id=p.merchant_id AND g.platform=p.platform
            AND g.platform_product_id=p.source_product_id
          WHERE s.barcode=ANY(:barcodes) AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
            AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
            AND g.product_group_id<>:group_id
        )
        """, {"barcodes": barcodes, "group_id": COHORT.canonical_group_id},
    ):
        raise ValueError("an additional AMUSE GTIN already serves from another product group")


async def apply_anchors(review):
    """Inside the same serializable transaction as the exact retailer write."""
    if os.getenv("MEITU_US_KIOKII_SIX_EXPANSION_ENABLED") != "1":
        raise ValueError("AMUSE six-shade expansion is disabled")
    await database.execute("SELECT product_key FROM catalog_products WHERE product_key=:product FOR UPDATE",
                           {"product": COHORT.canonical_product_key})
    row = await load_seed(lock=True)
    data, initial = seed_variants(row)
    await assert_unique_additions()
    if (review is None or _seed_review_version(row) != review.version
            or not 0 <= (datetime.now(timezone.utc) - review.captured_at).total_seconds()
            <= EVIDENCE_MAX_AGE.total_seconds()):
        raise ValueError("AMUSE seed version changed or machine evidence expired")
    if initial.keys() == EXPANDED_VARIANTS.keys():
        return {"existing": 6, "replayed": True, "rows": projection_summary(verify_projection(
            await canonical_rows(), EXPANDED_VARIANTS,
        ).values())}
    before = verify_projection(await canonical_rows(), INITIAL_VARIANTS)
    full_set = list(data["variants"]) + list(review.variants)
    result = await upsert_seed_data(
        seed_id=AMUSE_SEED_ID, external_product_id=EXTERNAL_ID, proposed_seed_data={"variants": full_set},
        proposer="machine_review:amuse_us_variant_expansion", source="meitu_us_official_variant_review",
        notes=json.dumps({"policy": POLICY_VERSION, "evidence_digest": review.evidence_digest,
                          "model_receipts": review.model_receipts}, sort_keys=True, default=str),
        project_after_merge=False,
    )
    if result.status != "merged" or result.audit_summary["review_status"] != "no_issues_found":
        raise ValueError("AMUSE variant proposal did not pass the audited seed data writer")
    updated = await load_seed()
    after, _ = seed_variants(updated)
    if after != {**data, "variants": full_set}:
        raise ValueError("AMUSE seed changed outside the reviewed variant addition")
    projection = await promote_variants_for_group(
        group_id=COHORT.canonical_group_id, apply=True,
        variant_ids=frozenset(EXPANDED_VARIANTS.keys() - INITIAL_VARIANTS.keys()),
    )
    if (projection.primary_product_key != COHORT.canonical_product_key or projection.variants_found != len(ADDITIONS)
            or projection.variant_offer_projection_failed):
        raise ValueError("AMUSE exact SKU and offer projection failed")
    if await refresh_agent_pdp_view_for_seed(
        seed_id=AMUSE_SEED_ID, proposal_id=result.proposal_id, refresh_source=POLICY_VERSION,
    ) != "refreshed":
        raise ValueError("AMUSE Commerce Index PDP refresh failed")
    anchors = verify_projection(await canonical_rows(), EXPANDED_VARIANTS)
    if any(anchors[native] != previous for native, previous in before.items()):
        raise ValueError("the original AMUSE anchors changed during projection")
    return {"existing": 2, "replayed": False, "proposal_id": result.proposal_id, "rows": projection_summary(anchors),
            "evidence_digest": review.evidence_digest}
