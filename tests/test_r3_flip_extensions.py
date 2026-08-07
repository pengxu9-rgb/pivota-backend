"""R3 — the flip-tool extensions for what reflection cannot see.

R0 measured two ownership tables outside `discover_cascade_tables`' reach:
`product_group_members` (no scope column) and `pdp_identity_listing` (merchant
embedded in its PRIMARY KEY, with 7,036 operator overrides + 515 review-queue
rows FK-attached to the old refs). Plus: retailer domains must key on etld1
alone (W2), and the retired test rig's 50 dev-store rows are excluded to their
own founder-gated step.
"""

import pytest

from scripts.backfill_seller_of_record import (
    BackfillReport,
    EXCLUDED_SOURCE_DOMAINS,
    SellerBackfill,
)
from services.seller_identity import make_observed_retailer_id


class _FakeDb:
    def __init__(self, *, products=None, listing_exists=False, pgm_target_exists=False):
        self.products = products or []
        self.listing_exists = listing_exists
        self.pgm_target_exists = pgm_target_exists
        self.executed = []

    async def fetch_all(self, sql, params=None):
        if "information_schema.columns" in sql and "pdp_identity_listing" in sql:
            return [{"column_name": c} for c in
                    ("source_listing_ref", "merchant_id", "product_id", "identity_status")]
        if "FROM information_schema.columns" in sql:
            return []  # cascade discovery: no reflected dependents in these tests
        if "FROM catalog_products cp" in sql and "cp.merchant_id = :banned" in sql:
            return self.products
        if "FROM catalog_products WHERE product_key" in sql:
            return [{"product_key": p["product_key"], "pivota_signature_id": "sig_x",
                     "pivota_canonical_url": "u", "content_key": "ck"} for p in self.products]
        if "FROM catalog_skus" in sql:
            return []
        return []

    async def fetch_one(self, sql, params=None):
        if "FROM pdp_identity_listing WHERE source_listing_ref" in sql:
            ref = (params or {}).get("r") or (params or {}).get("old_ref") or ""
            if str(ref).startswith("external_seed:"):
                return {"?column?": 1} if self.listing_exists else None
            return {"?column?": 1} if getattr(self, "new_ref_exists", False) else None
        if "count(*)" in sql:
            return {"c": 0}
        if "FROM product_group_members WHERE merchant_id = :obs" in sql:
            return {"product_group_id": "grp_obs"} if self.pgm_target_exists else None
        if "FROM product_group_members WHERE merchant_id = :banned" in sql:
            return {"product_group_id": "grp_banned"}
        return None

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), dict(params or {})))

    def transaction(self):
        class _Txn:
            async def __aenter__(self_inner): return self_inner
            async def __aexit__(self_inner, *a): return False
        return _Txn()


def _backfill(db):
    return SellerBackfill(database=db, si_mod=None, execute=False, batch_size=50)


ROW = {
    "product_key": "prod::external_seed::external_seed::x1",
    "content_key": "ck", "brand": "B", "source_domain": "b.com",
    "canonical_url": "https://b.com/p", "platform": "external_seed",
    "source_product_id": "x1",
    "seed_external_product_id": None, "seed_domain": None, "seed_destination_url": None,
}


class TestListingRefMigration:
    @pytest.mark.asyncio
    async def test_copy_repoint_delete_in_order(self):
        db = _FakeDb(listing_exists=True)
        bf = _backfill(db)
        await bf._migrate_listing_refs(ROW, "merch_obs_aaaa0000aaaa0000")
        sqls = [sql for sql, _ in db.executed]
        assert len(sqls) == 4
        # 1. copy under the new ref, idempotent for resume
        assert sqls[0].startswith("INSERT INTO pdp_identity_listing")
        assert "ON CONFLICT (source_listing_ref) DO NOTHING" in sqls[0]
        # every live column rides along (QUOTED against reserved words); the
        # two identity columns are replaced
        assert '"source_listing_ref", "merchant_id", "product_id", "identity_status"' in sqls[0]
        assert 'SELECT :new_ref, :obs, "product_id", "identity_status"' in sqls[0]
        # 2+3. BOTH child tables repoint — overrides are operator work
        assert "UPDATE pdp_identity_review_queue SET source_listing_ref" in sqls[1]
        assert "UPDATE pdp_identity_override SET source_listing_ref" in sqls[2]
        # 4. only then does the old row go
        assert sqls[3].startswith("DELETE FROM pdp_identity_listing")
        _, params = db.executed[0]
        assert params["old_ref"] == "external_seed:x1"
        assert params["new_ref"] == "merch_obs_aaaa0000aaaa0000:x1"

    @pytest.mark.asyncio
    async def test_noop_when_no_sentinel_listing(self):
        db = _FakeDb(listing_exists=False)
        await _backfill(db)._migrate_listing_refs(ROW, "merch_obs_aaaa0000aaaa0000")
        assert db.executed == []


class TestGroupMembership:
    @pytest.mark.asyncio
    async def test_moves_in_place_when_target_absent(self):
        db = _FakeDb(pgm_target_exists=False)
        await _backfill(db)._resubject_group_membership(ROW, "merch_obs_aaaa0000aaaa0000")
        assert len(db.executed) == 1
        sql, params = db.executed[0]
        assert sql.startswith("UPDATE product_group_members SET merchant_id = :obs")
        assert params["spid"] == "x1"

    @pytest.mark.asyncio
    async def test_retires_the_stale_row_when_target_exists(self):
        # The graph/mirror may already maintain the observed-seller membership;
        # moving the banned row onto it would violate the unique key. Self-heal:
        # the stale sentinel row is retired instead.
        db = _FakeDb(pgm_target_exists=True)
        await _backfill(db)._resubject_group_membership(ROW, "merch_obs_aaaa0000aaaa0000")
        assert len(db.executed) == 1
        sql, _ = db.executed[0]
        assert sql.startswith("DELETE FROM product_group_members WHERE merchant_id = :banned")


class TestPlanning:
    @pytest.mark.asyncio
    async def test_rig_rows_are_excluded_to_review_and_retailers_key_on_domain(self):
        rig_row = dict(ROW, product_key="prod::external_seed::external_seed::rig1",
                       source_domain="jwx893-fz.myshopify.com", source_product_id="rig1")
        ulta_row = dict(ROW, product_key="prod::external_seed::external_seed::u1",
                        source_domain="ulta.com", brand="Fenty Beauty", source_product_id="u1")
        db = _FakeDb(products=[rig_row, ulta_row])
        bf = _backfill(db)
        report = BackfillReport(mode='dry_run', started_at='2026-08-06T00:00:00Z', phases=['catalog'], batch_size=50)
        await bf.run_catalog(report)
        assert report.catalog["review_excluded_rig"] == 1
        assert report.catalog["retailer_keyed"] == 1
        reasons = {r["reason"] for r in report.review_queue["catalog_unresolvable"]}
        assert "excluded_test_rig_dev_store" in reasons
        # dry-run: one product planned, keyed on the retailer identity
        assert report.catalog["resubjected"] == 1

    def test_the_rig_domain_is_pinned(self):
        assert "jwx893-fz.myshopify.com" in EXCLUDED_SOURCE_DOMAINS

    def test_rig_exclusion_matches_www_variants_and_url_only_rows(self):
        from scripts.backfill_seller_of_record import _is_excluded_rig_row
        assert _is_excluded_rig_row({"source_domain": "www.jwx893-fz.myshopify.com"})
        assert _is_excluded_rig_row({"source_domain": "",
                                     "canonical_url": "https://jwx893-fz.myshopify.com/p/x"})
        assert not _is_excluded_rig_row({"source_domain": "b.com"})

    def test_retailer_identity_matches_w2_keying(self):
        assert make_observed_retailer_id("ulta.com").startswith("merch_obs_")


class TestPlanDomain:
    def test_garbage_source_domain_does_not_mask_a_good_canonical_host(self):
        from scripts.backfill_seller_of_record import _plan_domain
        row = {"brand": "Biodance", "source_domain": "Better Formula for Better Glow",
               "canonical_url": "https://biodance.com/products/x"}
        assert _plan_domain(row) == ("biodance.com", True)

    def test_seed_destination_recovers_when_catalog_fields_are_garbage(self):
        # The 250-row Biodance cohort: tagline in source_domain, empty
        # canonical_url, real domain only on the attached seed.
        from scripts.backfill_seller_of_record import _plan_domain
        row = {"brand": "Biodance", "source_domain": "Better Formula for Better Glow",
               "canonical_url": "", "seed_domain": "",
               "seed_destination_url": "https://biodance.com/products/x"}
        assert _plan_domain(row) == ("biodance.com", True)

    def test_valid_source_domain_wins(self):
        from scripts.backfill_seller_of_record import _plan_domain
        row = {"brand": "B", "source_domain": "b.com",
               "canonical_url": "https://retailer.example/products/x"}
        assert _plan_domain(row) == ("b.com", True)

    def test_nothing_resolvable_reports_unresolved(self):
        from scripts.backfill_seller_of_record import _plan_domain
        row = {"brand": "", "source_domain": "not a domain", "canonical_url": ""}
        domain, resolved = _plan_domain(row)
        assert resolved is False

    def test_a_bare_public_suffix_never_reaches_the_brand_resolver(self):
        # make_observed_merchant_id lacks the plausible-registrable guard and
        # happily keys on a bare 'com.vn' — the plan must route to review
        # instead of falling through (review finding 7).
        from scripts.backfill_seller_of_record import _plan_domain
        row = {"brand": "Brand", "source_domain": "shop.com.vn", "canonical_url": ""}
        _domain, resolved = _plan_domain(row)
        assert resolved is False


class TestExecuteDivergenceGuard:
    @pytest.mark.asyncio
    async def test_ensure_gets_the_planned_domain_and_divergence_skips_the_product(self, monkeypatch):
        # Review finding 1: execute used to feed ensure the RAW columns while
        # the row was re-keyed to the PLANNED id — the Biodance cohort would
        # have crashed the batch, and claim-attach divergence would have keyed
        # rows to a merchant that never got minted.
        import scripts.backfill_seller_of_record as mod

        calls = {}
        async def fake_ensure(*, kind, brand, domain, source_system, primary_platform=None):
            calls["domain"] = domain
            return "merch_obs_DIFFERENT0000000"  # diverges from the plan
        monkeypatch.setattr(mod, "ensure_observed_seller_of_record", fake_ensure)

        db = _FakeDb(products=[])
        bf = SellerBackfill(database=db, si_mod=None, execute=True, batch_size=10)
        report = BackfillReport(mode='execute', started_at='2026-08-07T00:00:00Z',
                                phases=['catalog'], batch_size=10)
        batch = [dict(ROW, observed_id="merch_obs_planned000000000",
                      seller_kind="brand_direct", seller_domain="biodance.com",
                      listing_product_ids=["x1"])]
        parity = await bf._resubject_batch(batch, [], report)
        assert calls["domain"] == "biodance.com"          # the PLANNED domain
        assert parity["skipped_to_review"][0]["reason"] == "ensured_seller_diverges_from_plan"
        # No catalog UPDATE ran for the skipped product.
        assert not any("UPDATE catalog_products" in sql for sql, _ in db.executed)


class TestListingCandidates:
    def test_path_c_listing_refs_use_the_seed_external_id(self):
        # Review finding 2: Path C rows carry a name slug in source_product_id;
        # their listings key on the attached seed's external_product_id.
        # Measured 2026-08-07: 0 Path C listings under the slug, 2,444 under
        # the seed id.
        db = _FakeDb()
        bf = _backfill(db)
        b = dict(ROW, source_product_id="brand name slug",
                 listing_product_ids=["brand name slug", "ext_seed_123"])
        refs = bf._listing_refs_for(b, "merch_obs_aaaa0000aaaa0000")
        assert ("external_seed:ext_seed_123", "merch_obs_aaaa0000aaaa0000:ext_seed_123") in refs
        assert ("external_seed:brand name slug", "merch_obs_aaaa0000aaaa0000:brand name slug") in refs


class TestRetailerSeedRederivation:
    def test_ulta_seed_with_per_brand_ref_replans_to_the_retailer_id(self):
        from scripts.rederive_retailer_seed_seller_refs import plan_seed
        from services.seller_identity import make_observed_retailer_id
        p = plan_seed({"id": "s1", "domain": "ulta.com", "brand": "Fenty Beauty",
                       "seller_ref": "merch_obs_039b8cd5c84730bc"})
        assert p is not None
        assert p["new"] == make_observed_retailer_id("ulta.com")

    def test_brand_direct_seed_is_never_touched(self):
        from scripts.rederive_retailer_seed_seller_refs import plan_seed
        assert plan_seed({"id": "s2", "domain": "fentybeauty.com",
                          "brand": "Fenty Beauty", "seller_ref": "merch_obs_x"}) is None

    def test_already_correct_retailer_ref_is_a_noop(self):
        from scripts.rederive_retailer_seed_seller_refs import plan_seed
        from services.seller_identity import make_observed_retailer_id
        rid = make_observed_retailer_id("ulta.com")
        assert plan_seed({"id": "s3", "domain": "ulta.com", "brand": "X",
                          "seller_ref": rid}) is None

    def test_unresolvable_domain_leaves_the_seed_alone(self):
        from scripts.rederive_retailer_seed_seller_refs import plan_seed
        assert plan_seed({"id": "s4", "domain": "not a domain",
                          "brand": "", "seller_ref": "merch_obs_y"}) is None

    def test_retailer_domain_recovered_from_destination_url(self):
        from scripts.rederive_retailer_seed_seller_refs import plan_seed
        from services.seller_identity import make_observed_retailer_id
        p = plan_seed({"id": "s5", "domain": "", "seller_ref": "",
                       "destination_url": "https://www.ulta.com/p/x", "brand": "B"})
        assert p is not None and p["new"] == make_observed_retailer_id("ulta.com")


class TestMaxBatches:
    @pytest.mark.asyncio
    async def test_canary_stops_after_the_cap(self):
        rows = [dict(ROW, product_key=f"prod::external_seed::external_seed::c{i}",
                     source_product_id=f"c{i}", source_domain="ulta.com") for i in range(5)]
        db = _FakeDb(products=rows)
        bf = SellerBackfill(database=db, si_mod=None, execute=False, batch_size=2, max_batches=1)
        report = BackfillReport(mode='dry_run', started_at='2026-08-07T00:00:00Z',
                                phases=['catalog'], batch_size=2)
        await bf.run_catalog(report)
        assert report.catalog["batches"] == 1
        assert report.catalog["stopped_at_max_batches"] == 1
        assert report.catalog["resubjected"] == 2   # one batch of two, not five
