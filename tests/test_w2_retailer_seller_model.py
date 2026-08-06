"""W2 (ADR-011 R5 closure) — the Path C writer mints the seller of record.

The retailer keying rule, founder-ratified 2026-08-05:
  retailer domain (offer_seller_identity's known list) -> keyed on etld1 ALONE
  brand-direct domain                                  -> (brand, etld1), unchanged
  neither resolvable                                   -> the record is BLOCKED
                                                          (skipped + retried),
                                                          NEVER the legacy bucket

These tests exercise the PURE layer (classification, keying, plan building) —
the claimed-attach and insert-if-missing behavior live at apply time and are
covered by the tripwire test at the bottom.
"""

import pytest

from services.seller_identity import (
    BANNED_BUCKET_MERCHANT_ID,
    is_known_retailer_domain,
    make_observed_merchant_id,
    make_observed_retailer_id,
    resolve_seed_seller_identity,
)
from services.catalog_enrichment_agent.ingestion import (
    ingest_validated_jsonl,
    ingest_validated_record,
)


def _record(*, brand="Fenty Beauty", domain="fentybeauty.com", name="Gloss Bomb"):
    return {
        "pdp": {
            "brand": brand,
            "product_name": name,
            "source_domain": domain,
            "category_path": "beauty/lip",
        },
        "offers": [
            {
                "canonical_url": f"https://{domain}/p/gloss-bomb",
                "destination_url": f"https://{domain}/p/gloss-bomb",
                "merchant_inferred": None,
                "price": 21.0,
                "in_stock": True,
                "validated": True,
                "url_valid": True,
            }
        ],
    }


class TestRetailerKeying:
    def test_one_retailer_is_one_merchant_regardless_of_brand(self):
        a = resolve_seed_seller_identity(brand="Fenty Beauty", domain="ulta.com")
        b = resolve_seed_seller_identity(brand="The Ordinary", domain="www.ulta.com")
        assert a["kind"] == "retailer"
        assert a["merchant_id"] == b["merchant_id"]
        assert a["merchant_id"].startswith("merch_obs_")

    def test_retailer_subdomain_classifies_by_host_or_registrable(self):
        assert is_known_retailer_domain("global.oliveyoung.com")
        assert is_known_retailer_domain("www.sephora.com")
        assert not is_known_retailer_domain("fentybeauty.com")

    def test_retailer_and_brand_direct_id_spaces_are_disjoint(self):
        # Same domain string fed to both mints must never collide: the
        # retailer namespace is prefixed before hashing.
        retailer = make_observed_retailer_id("ulta.com")
        brand_shaped = make_observed_merchant_id("ulta", "ulta.com")
        assert retailer != brand_shaped

    def test_brand_direct_unchanged_from_adr009(self):
        r = resolve_seed_seller_identity(brand="Fenty Beauty", domain="fentybeauty.com")
        assert r["kind"] == "brand_direct"
        assert r["merchant_id"] == make_observed_merchant_id("Fenty Beauty", "fentybeauty.com")


class TestNoFallback:
    def test_empty_brand_on_brand_direct_domain_raises(self):
        with pytest.raises(ValueError):
            resolve_seed_seller_identity(brand="", domain="someunknownbrand.com")

    def test_unregistrable_domain_raises(self):
        with pytest.raises(ValueError):
            resolve_seed_seller_identity(brand="X", domain="")

    def test_empty_brand_on_a_RETAILER_domain_still_resolves(self):
        # The whole point of retailer keying: the brand is a product attribute,
        # not part of the seller identity.
        r = resolve_seed_seller_identity(brand="", domain="ulta.com")
        assert r["kind"] == "retailer"


class TestPlanBuilding:
    def test_retailer_record_mints_retailer_id_on_product_and_sku(self):
        plan = ingest_validated_jsonl([_record(brand="Fenty Beauty", domain="ulta.com")])
        expected = make_observed_retailer_id("ulta.com")
        assert plan["pdps"][0]["merchant_id"] == expected
        assert plan["skus"][0]["merchant_id"] == expected

    def test_brand_direct_record_mints_brand_id(self):
        plan = ingest_validated_jsonl([_record(brand="Fenty Beauty", domain="fentybeauty.com")])
        expected = make_observed_merchant_id("Fenty Beauty", "fentybeauty.com")
        assert plan["pdps"][0]["merchant_id"] == expected

    def test_the_observed_seller_row_rides_in_the_merchants_list_as_ensure_only(self):
        plan = ingest_validated_jsonl([_record(domain="ulta.com")])
        ensure_rows = [m for m in plan["merchants"] if m.get("_ensure_only")]
        assert len(ensure_rows) == 1
        assert ensure_rows[0]["merchant_id"] == make_observed_retailer_id("ulta.com")
        assert ensure_rows[0]["status"] == "observed"

    def test_unresolvable_record_is_BLOCKED_not_bucketed(self):
        # A real brand but no registrable domain: the payload passes the intake
        # gate, and the seller resolver is what blocks it.
        plan = ingest_validated_jsonl([_record(brand="RealBrand", domain="localhost")])
        assert plan["pdps"] == []
        assert plan["skipped"] == 1
        assert plan["skipped_reasons"].get("seller_of_record_unresolved") == 1

    def test_no_plan_row_ever_carries_the_banned_bucket(self):
        plans = [
            ingest_validated_jsonl([_record(domain="ulta.com")]),
            ingest_validated_jsonl([_record(domain="fentybeauty.com")]),
            ingest_validated_jsonl([_record(brand="RealBrand", domain="localhost")]),
        ]
        for plan in plans:
            for row in plan["pdps"] + plan["skus"]:
                assert row["merchant_id"] != BANNED_BUCKET_MERCHANT_ID

    def test_sig_and_key_derivations_are_untouched_storage_tokens(self):
        # Sigs are write-once (T5) and product_key is opaque plumbing (D4.2):
        # the seller change must not alter either derivation.
        plan = ingest_validated_jsonl([_record(domain="ulta.com")])
        row = plan["pdps"][0]
        assert row["product_key"].startswith("ext:")
        assert row["pivota_signature_id"]

    def test_seller_never_derives_from_offer_order(self):
        # A PDP with no source_domain is BLOCKED even when its offers have
        # perfectly good domains: deriving from offers[0] would make the seller
        # identity depend on offer ordering — a wrong-but-plausible fallback.
        rec = _record(domain="ulta.com")
        del rec["pdp"]["source_domain"]
        plan = ingest_validated_jsonl([rec])
        assert plan["pdps"] == []
        assert plan["skipped_reasons"].get("seller_of_record_unresolved") == 1

    def test_skipped_reason_shape_survives_a_none_record(self):
        # A malformed record (no offers) still counts as a plain skip.
        rec = _record()
        rec["offers"] = []
        plan = ingest_validated_jsonl([rec])
        assert plan["skipped"] == 1
        assert plan["skipped_reasons"] == {}


class TestApplyTripwire:
    @pytest.mark.asyncio
    async def test_apply_refuses_a_banned_bucket_row(self):
        from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

        class _NeverCalledDb:
            async def execute(self, *_args, **_kwargs):
                raise AssertionError("database.execute must not be reached")

        plan = {
            "pdps": [{"product_key": "ext:x::1", "merchant_id": BANNED_BUCKET_MERCHANT_ID}],
            "skus": [],
            "merchants": [],
        }
        with pytest.raises(RuntimeError, match="ADR-009 D2"):
            await _prepare_seller_of_record(plan, _NeverCalledDb())
