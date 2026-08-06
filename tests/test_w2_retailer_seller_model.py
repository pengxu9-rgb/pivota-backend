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




class _FakeDb:
    """Records every call; programmable responses keyed by SQL substring."""

    def __init__(self, *, existing_products=None, claims=None, claimed_merchant_rows=None):
        self.executed = []
        self._existing = existing_products or []      # [{product_key, merchant_id}]
        self._claims = claims or {}                   # registrable -> merchant_id
        self._claimed_rows = set(claimed_merchant_rows or [])

    async def fetch_all(self, sql, params=None):
        if "FROM catalog_products" in sql:
            keys = set((params or {}).get("keys") or [])
            return [r for r in self._existing if r["product_key"] in keys]
        return []

    async def fetch_one(self, sql, params=None):
        if "FROM brand_claims" in sql:
            mid = self._claims.get((params or {}).get("reg"))
            return {"merchant_id": mid} if mid else None
        if "FROM catalog_merchants" in sql:
            return {"?column?": 1} if (params or {}).get("mid") in self._claimed_rows else None
        return None

    async def execute(self, sql, params=None):
        self.executed.append((sql, dict(params or {})))


def _plan(*, product_key="ext:brand snail::abc12345", merchant_id, ensure_id=None, registrable="ulta.com"):
    merchants = []
    if ensure_id:
        merchants.append({
            "merchant_id": ensure_id, "merchant_name": registrable,
            "primary_platform": "external_seed", "status": "observed",
            "source_system": "x", "source_ref": registrable,
            "metadata_json": "{}", "_ensure_only": True,
        })
    return {
        "pdps": [{"product_key": product_key, "merchant_id": merchant_id}],
        "skus": [{"sku_key": product_key + "::canonical", "product_key": product_key, "merchant_id": merchant_id}],
        "merchants": merchants,
    }


class TestPrepareSellerOfRecord:
    @pytest.mark.asyncio
    async def test_existing_rows_win_even_when_sentinel(self):
        # The upsert never updates merchant_id, so plan rows for an existing
        # product must follow the DB row — otherwise the pg-membership singleton
        # and the observed merchant row assert an ownership the catalog does
        # not have (phantom rows at legacy-population scale).
        from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

        db = _FakeDb(existing_products=[{"product_key": "ext:x::1", "merchant_id": "external_seed"}])
        plan = _plan(product_key="ext:x::1", merchant_id="merch_obs_aaaa0000aaaa0000", ensure_id="merch_obs_aaaa0000aaaa0000")
        out = await _prepare_seller_of_record(plan, db)
        assert plan["pdps"][0]["merchant_id"] == "external_seed"
        assert plan["skus"][0]["merchant_id"] == "external_seed"
        # The now-unreferenced observed merchant row is NOT inserted.
        assert db.executed == []
        assert out["merchants"] == []

    @pytest.mark.asyncio
    async def test_tripwire_fires_only_for_NEW_sentinel_rows(self):
        from services.catalog_enrichment_agent.apply import _prepare_seller_of_record
        from services.seller_identity import BANNED_BUCKET_MERCHANT_ID

        # New product under the bucket -> refused.
        with pytest.raises(RuntimeError, match="ADR-009 D2"):
            await _prepare_seller_of_record(
                _plan(product_key="ext:new::9", merchant_id=BANNED_BUCKET_MERCHANT_ID), _FakeDb(),
            )
        # Existing sentinel row remapped by existing-wins -> allowed.
        db = _FakeDb(existing_products=[{"product_key": "ext:x::1", "merchant_id": BANNED_BUCKET_MERCHANT_ID}])
        await _prepare_seller_of_record(
            _plan(product_key="ext:x::1", merchant_id="merch_obs_bbbb0000bbbb0000"), db,
        )

    @pytest.mark.asyncio
    async def test_ensure_only_flag_never_reaches_a_sql_bind(self):
        from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

        db = _FakeDb()
        await _prepare_seller_of_record(
            _plan(merchant_id="merch_obs_cccc0000cccc0000", ensure_id="merch_obs_cccc0000cccc0000"), db,
        )
        assert len(db.executed) == 1
        _sql, params = db.executed[0]
        assert "_ensure_only" not in params

    @pytest.mark.asyncio
    async def test_claimed_attach_remaps_when_tenant_merchant_exists(self):
        from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

        db = _FakeDb(claims={"ulta.com": "merch_tenant_1"}, claimed_merchant_rows={"merch_tenant_1"})
        plan = _plan(merchant_id="merch_obs_dddd0000dddd0000", ensure_id="merch_obs_dddd0000dddd0000")
        out = await _prepare_seller_of_record(plan, db)
        assert plan["pdps"][0]["merchant_id"] == "merch_tenant_1"
        assert plan["skus"][0]["merchant_id"] == "merch_tenant_1"
        assert db.executed == []          # no observed row written
        assert out["merchants"] == []

    @pytest.mark.asyncio
    async def test_claimed_attach_deferred_when_tenant_merchant_row_missing(self):
        # Serving surfaces INNER-JOIN catalog_merchants; remapping onto a
        # missing row would silently hide the products. The observed identity
        # stands and the attach is deferred loudly.
        from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

        db = _FakeDb(claims={"ulta.com": "merch_tenant_2"})  # no catalog row
        plan = _plan(merchant_id="merch_obs_eeee0000eeee0000", ensure_id="merch_obs_eeee0000eeee0000")
        await _prepare_seller_of_record(plan, db)
        assert plan["pdps"][0]["merchant_id"] == "merch_obs_eeee0000eeee0000"
        assert len(db.executed) == 1      # observed row inserted

    def test_prepare_runs_before_the_batched_dispatch(self):
        # Moving the prepare call below `if batch:` would silently regress the
        # batched executor with zero behavioral test failures.
        import inspect
        from services.catalog_enrichment_agent import apply as apply_mod

        src = inspect.getsource(apply_mod.apply_ingest_plan)
        assert src.index("_prepare_seller_of_record") < src.index("if batch:")


class TestLiveProducersCarrySourceDomain:
    @pytest.mark.asyncio
    async def test_gemini_validator_mock_path_sets_source_domain(self):
        # F1 regression pin: the live validator lane must carry source_domain
        # or every record it produces is blocked at seller resolution.
        from services.catalog_enrichment_agent.gemini_url_validator import validate_candidate

        result = await validate_candidate(
            {
                "brand": "COSRX",
                "product_name": "Snail Mucin Essence",
                "category_path": "beauty/skin",
                "attribute_summary": "x",
                "expected_url_domains": ["www.cosrx.com"],
            },
            api_key="",
        )
        assert result["pdp"]["source_domain"] == "cosrx.com"

    def test_runner_exception_fallback_sets_source_domain(self):
        import inspect
        from services.catalog_enrichment_agent import runner as runner_mod

        src = inspect.getsource(runner_mod._validate_all)
        assert '"source_domain"' in src
