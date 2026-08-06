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
        if "FROM catalog_products WHERE merchant_id" in sql:
            return self.products
        if "FROM catalog_products WHERE product_key" in sql:
            return [{"product_key": p["product_key"], "pivota_signature_id": "sig_x",
                     "pivota_canonical_url": "u", "content_key": "ck"} for p in self.products]
        if "FROM catalog_skus" in sql:
            return []
        return []

    async def fetch_one(self, sql, params=None):
        if "FROM pdp_identity_listing WHERE source_listing_ref" in sql:
            return {"?column?": 1} if self.listing_exists else None
        if "FROM product_group_members WHERE merchant_id = :obs" in sql:
            return {"?column?": 1} if self.pgm_target_exists else None
        if "count(*)" in sql:
            return {"c": 0}
        return None

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), dict(params or {})))


def _backfill(db):
    return SellerBackfill(database=db, si_mod=None, execute=False, batch_size=50)


ROW = {
    "product_key": "prod::external_seed::external_seed::x1",
    "content_key": "ck", "brand": "B", "source_domain": "b.com",
    "canonical_url": "https://b.com/p", "platform": "external_seed",
    "source_product_id": "x1",
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
        # every live column rides along; the two identity columns are replaced
        assert "source_listing_ref, merchant_id, product_id, identity_status" in sqls[0]
        assert "SELECT :new_ref, :obs, product_id, identity_status" in sqls[0]
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

    def test_retailer_identity_matches_w2_keying(self):
        assert make_observed_retailer_id("ulta.com").startswith("merch_obs_")


class TestPlanDomain:
    def test_garbage_source_domain_does_not_mask_a_good_canonical_host(self):
        from scripts.backfill_seller_of_record import _plan_domain
        row = {"brand": "Biodance", "source_domain": "Better Formula for Better Glow",
               "canonical_url": "https://biodance.com/products/x"}
        assert _plan_domain(row) == "biodance.com"

    def test_valid_source_domain_wins(self):
        from scripts.backfill_seller_of_record import _plan_domain
        row = {"brand": "B", "source_domain": "b.com",
               "canonical_url": "https://retailer.example/products/x"}
        assert _plan_domain(row) == "b.com"

    def test_nothing_resolvable_still_blocks(self):
        from scripts.backfill_seller_of_record import _plan_domain
        row = {"brand": "", "source_domain": "not a domain", "canonical_url": ""}
        # returns the raw candidate; resolve_seed_seller_identity then raises ->
        # the row routes to review, never to a guessed identity.
        assert _plan_domain(row) == "not a domain"
