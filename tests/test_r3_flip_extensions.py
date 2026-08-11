"""R3 — the flip-tool extensions for what reflection cannot see.

R0 measured two ownership tables outside `discover_cascade_tables`' reach:
`product_group_members` (no scope column) and `pdp_identity_listing` (merchant
embedded in its PRIMARY KEY, with 7,036 operator overrides + 515 review-queue
rows FK-attached to the old refs). Plus: retailer domains must key on etld1
alone (W2), and the retired test rig's 50 dev-store rows are excluded to their
own founder-gated step.
"""

import re

import pytest

from scripts.backfill_seller_of_record import (
    BackfillReport,
    EXCLUDED_SOURCE_DOMAINS,
    SellerBackfill,
)
from services.seller_identity import make_observed_retailer_id


def _assert_binds_match(sql, params):
    """`databases` binds through SQLAlchemy text(): a params dict carrying a key
    the statement does not name raises ArgumentError, and a named bind with no
    value raises too. The old fake recorded SQL and params without ever
    comparing them, so a real ArgumentError shipped to production and aborted
    the canary batch (2026-08-07). Every fake query now enforces exact equality.
    """
    named = set(re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", sql))
    given = set((params or {}).keys())
    assert named == given, (
        f"bind mismatch: SQL names {sorted(named)}, params give {sorted(given)}"
        f" (extra={sorted(given - named)}, missing={sorted(named - given)})\nSQL: {sql}"
    )


class _FakeDb:
    def __init__(self, *, products=None, listing_exists=False, pgm_target_exists=False,
                 cascade=None, pgm_banned_rows=None):
        # None -> one banned row keyed on the first hunted pid (the pre-fix
        # single-row world); [] -> no banned rows; list -> exactly those.
        self.pgm_banned_rows = pgm_banned_rows
        self.cascade = cascade or []
        self.products = products or []
        self.listing_exists = listing_exists
        self.pgm_target_exists = pgm_target_exists
        self.executed = []

    async def fetch_all(self, sql, params=None):
        _assert_binds_match(sql, params)
        if "information_schema.columns" in sql and "pdp_identity_listing" in sql:
            return [{"column_name": c} for c in
                    ("source_listing_ref", "merchant_id", "product_id", "identity_status")]
        if "FROM information_schema.columns" in sql:
            # Cascade discovery. Tests that pass `cascade=` exercise the
            # dependent-table path (_resubject_dependent) — it executed in NO
            # test before 2026-08-07, which is how a NameError reached prod.
            return [{"table_name": t, "column_name": c}
                    for t, cols in self.cascade for c in cols]
        if "FROM catalog_products cp" in sql and "cp.merchant_id = :banned" in sql:
            return self.products
        if "FROM catalog_products WHERE product_key" in sql:
            return [{"product_key": p["product_key"], "pivota_signature_id": "sig_x",
                     "pivota_canonical_url": "u", "content_key": "ck"} for p in self.products]
        if "FROM catalog_skus" in sql:
            return []
        if "FROM product_group_members" in sql and "ANY(:pids)" in sql:
            if self.pgm_banned_rows is None:
                pid = (params or {}).get("pids", [""])[0]
                return [{"platform": "external_seed", "platform_product_id": pid,
                         "product_group_id": "grp_banned"}]
            return self.pgm_banned_rows
        return []

    async def fetch_one(self, sql, params=None):
        _assert_binds_match(sql, params)
        if "FROM pdp_identity_listing WHERE source_listing_ref" in sql:
            ref = (params or {}).get("r") or (params or {}).get("old_ref") or ""
            if str(ref).startswith("external_seed:"):
                return {"?column?": 1} if self.listing_exists else None
            return {"?column?": 1} if getattr(self, "new_ref_exists", False) else None
        if "count(*)" in sql and "FROM product_group_members" in sql:
            rows = self.pgm_banned_rows
            n = 1 if rows is None else len(rows)
            return {"c": n}
        if "count(*)" in sql:
            return {"c": 0}
        if "FROM product_group_members WHERE merchant_id = :obs" in sql:
            return {"product_group_id": "grp_obs"} if self.pgm_target_exists else None
        if "FROM product_group_members WHERE merchant_id = :banned" in sql:
            return {"product_group_id": "grp_banned"}
        return None

    async def execute(self, sql, params=None):
        _assert_binds_match(sql, params)
        self.executed.append((" ".join(sql.split()), dict(params or {})))

    def transaction(self):
        class _Txn:
            async def __aenter__(self_inner): return self_inner
            async def __aexit__(self_inner, *a): return False
        return _Txn()

    # _retry_after_reset's hard reset; counted so tests can assert it ran.
    async def disconnect(self):
        self.resets = getattr(self, "resets", 0) + 1

    async def connect(self):
        pass


@pytest.fixture(autouse=True)
def _healthy_fragmentation_guard(monkeypatch):
    """The guard's lookup PROPAGATES failures now. Against the sqlite test
    fixture the real ILIKE query is a hard error — which the old swallow
    silently converted to "no collision", so every planning test here ran
    with the guard disarmed without knowing it. Default to an explicit
    healthy "no conflict"; the door tests override per-case."""
    import services.audit_index_intake as aii

    async def _no_conflict(*a, **k):
        return None

    monkeypatch.setattr(aii, "_existing_brand_canonical_conflict", _no_conflict)


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
        batch = [dict(ROW, observed_id="merch_obs_planned000000000",
                      seller_kind="brand_direct", seller_domain="biodance.com",
                      listing_product_ids=["x1"])]
        parity = await bf._resubject_batch(batch, [])
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


class TestMaxProducts:
    @pytest.mark.asyncio
    async def test_scan_cap_reaches_the_sql(self):
        captured = {}
        class _Db(_FakeDb):
            async def fetch_all(self, sql, params=None):
                if "cp.merchant_id = :banned" in sql and "FROM catalog_products cp" in sql:
                    captured["sql"] = sql; captured["params"] = dict(params or {})
                    return []
                return await super().fetch_all(sql, params)
        bf = SellerBackfill(database=_Db(), si_mod=None, execute=False,
                            batch_size=25, max_products=30)
        report = BackfillReport(mode='dry_run', started_at='2026-08-07T00:00:00Z',
                                phases=['catalog'], batch_size=25)
        await bf.run_catalog(report)
        assert "LIMIT :cap" in captured["sql"]
        assert captured["params"]["cap"] == 30


class TestDependentCascade:
    """_resubject_dependent runs ONLY when reflection finds dependent tables —
    so every earlier fake (which returned no cascade) left it unexecuted, and a
    NameError shipped. These exercise all three scope shapes for real."""

    @pytest.mark.asyncio
    async def test_product_key_scope_executes_with_correct_binds(self):
        db = _FakeDb(cascade=[("catalog_skus", ["merchant_id", "product_key"])])
        bf = _backfill(db)
        cascade = await bf.discover_cascade_tables()
        assert cascade and cascade[0]["scope_col"] == "product_key"
        await bf._resubject_dependent(cascade[0], "merch_obs_aaaa0000aaaa0000",
                                      "prod::external_seed::external_seed::x1", "ck", ["sku1"])
        sql, params = db.executed[-1]
        assert sql.startswith("UPDATE catalog_skus SET merchant_id = :obs")
        assert set(params) == {"obs", "scope", "banned"}

    @pytest.mark.asyncio
    async def test_content_key_scope_executes_with_correct_binds(self):
        db = _FakeDb(cascade=[("index_pipeline_state", ["merchant_id", "content_key"])])
        bf = _backfill(db)
        cascade = await bf.discover_cascade_tables()
        assert cascade[0]["scope_col"] == "content_key"
        await bf._resubject_dependent(cascade[0], "merch_obs_aaaa0000aaaa0000",
                                      "prod::external_seed::external_seed::x1", "ck", [])
        sql, params = db.executed[-1]
        assert "WHERE content_key = :scope" in sql
        assert set(params) == {"obs", "scope", "banned"}

    @pytest.mark.asyncio
    async def test_sku_key_scope_executes_with_correct_binds(self):
        db = _FakeDb(cascade=[("beauty_sku_ingredients", ["merchant_id", "sku_key"])])
        bf = _backfill(db)
        cascade = await bf.discover_cascade_tables()
        assert cascade[0]["scope_col"] == "sku_key"
        await bf._resubject_dependent(cascade[0], "merch_obs_aaaa0000aaaa0000",
                                      "prod::external_seed::external_seed::x1", "ck", ["sku1"])
        sql, params = db.executed[-1]
        assert "WHERE sku_key = ANY(:scope)" in sql
        assert set(params) == {"obs", "scope", "banned"}

    @pytest.mark.asyncio
    async def test_a_full_batch_walks_the_dependent_path_end_to_end(self):
        # The integration shape the canary hit: a real cascade table means
        # _resubject_batch calls _resubject_dependent for every product.
        db = _FakeDb(products=[dict(ROW, source_domain="ulta.com")],
                     cascade=[("catalog_skus", ["merchant_id", "product_key"])])
        bf = SellerBackfill(database=db, si_mod=None, execute=False, batch_size=25)
        report = BackfillReport(mode="dry_run", started_at="2026-08-07T00:00:00Z",
                                phases=["catalog"], batch_size=25)
        await bf.run_catalog(report)
        assert report.catalog["resubjected"] == 1


class TestMultiSeedListings:
    @pytest.mark.asyncio
    async def test_every_active_seed_listing_migrates_not_just_the_picked_one(self):
        # The canary's 4-seed Anua product migrated only the DISTINCT ON pick's
        # listing, stranding 3 (verifier catch, 2026-08-08). The plan must carry
        # ALL active attached seed ids as listing candidates.
        rows = [dict(ROW, source_domain="ulta.com",
                     seed_external_product_id="anua:pick",
                     seed_listing_ids=["anua:pick", "anua:b", "anua:c", "anua:d"])]
        db = _FakeDb(products=rows)
        bf = _backfill(db)
        report = BackfillReport(mode="dry_run", started_at="2026-08-08T00:00:00Z",
                                phases=["catalog"], batch_size=25)
        await bf.run_catalog(report)
        # reach into the dry-run's planned listing candidates via the batch call:
        # simplest observable — re-plan the row shape directly
        b = {"source_product_id": "x1",
             "listing_product_ids": ["x1", "anua:pick", "anua:b", "anua:c", "anua:d"]}
        refs = bf._listing_refs_for(b, "merch_obs_aaaa0000aaaa0000")
        assert len(refs) == 5
        assert ("external_seed:anua:d", "merch_obs_aaaa0000aaaa0000:anua:d") in refs


class TestRepairListings:
    @pytest.mark.asyncio
    async def test_repair_migrates_stragglers_for_checkpointed_products(self):
        class _RepairDb(_FakeDb):
            async def fetch_all(self, sql, params=None):
                _assert_binds_match(sql, params)
                if "FROM a9_4_backfill_checkpoint ck" in sql:
                    return [{"product_key": "prod::external_seed::external_seed::x1",
                             "observed_id": "merch_obs_aaaa0000aaaa0000",
                             "source_product_id": "x1",
                             "seed_listing_ids": ["anua:b"]}]
                if "information_schema.columns" in sql and "pdp_identity_listing" in sql:
                    return [{"column_name": c} for c in
                            ("source_listing_ref", "merchant_id", "product_id")]
                return []
        db = _RepairDb(listing_exists=True)
        bf = SellerBackfill(database=db, si_mod=None, execute=True, batch_size=25)
        report = BackfillReport(mode="execute", started_at="2026-08-08T00:00:00Z",
                                phases=["repair-listings"], batch_size=25)
        await bf.repair_listings(report)
        stats = report.catalog["listing_repair"]
        assert stats["stale_found"] == 2          # x1 + anua:b old refs both present
        assert stats["migrated"] == 2
        # migration statements actually executed
        assert any("INSERT INTO pdp_identity_listing" in sql for sql, _ in db.executed)
        assert any("DELETE FROM pdp_identity_listing" in sql for sql, _ in db.executed)

    @pytest.mark.asyncio
    async def test_repair_dry_run_reports_without_writing(self):
        class _RepairDb(_FakeDb):
            async def fetch_all(self, sql, params=None):
                _assert_binds_match(sql, params)
                if "FROM a9_4_backfill_checkpoint ck" in sql:
                    return [{"product_key": "prod::external_seed::external_seed::x1",
                             "observed_id": "merch_obs_aaaa0000aaaa0000",
                             "source_product_id": "x1", "seed_listing_ids": None}]
                return []
        db = _RepairDb(listing_exists=True)
        bf = SellerBackfill(database=db, si_mod=None, execute=False, batch_size=25)
        report = BackfillReport(mode="dry_run", started_at="2026-08-08T00:00:00Z",
                                phases=["repair-listings"], batch_size=25)
        await bf.repair_listings(report)
        assert report.catalog["listing_repair"]["stale_found"] == 1
        assert report.catalog["listing_repair"]["migrated"] == 0
        assert db.executed == []


class TestFailFastDoors:
    """2026-08-09, run 31345305445: a proxy socket died mid-query, two
    warn-and-continue guards swallowed it, and the rest of the run planned
    with its safety checks answering "safe" on a wedged connection. Every
    door must answer BOTH ways: a working lookup answers the question, a
    broken lookup ABORTS — never defaults to the permissive answer."""

    @pytest.mark.asyncio
    async def test_fragmentation_guard_propagates_lookup_failure(self, monkeypatch):
        import services.audit_index_intake as aii

        async def _dead_socket(*a, **k):
            raise RuntimeError("connection was closed in the middle of operation")

        monkeypatch.setattr(aii, "_existing_brand_canonical_conflict", _dead_socket)
        bf = _backfill(_FakeDb())
        with pytest.raises(RuntimeError, match="closed in the middle"):
            await bf._fragmentation_collision("merch_obs_aaaa0000aaaa0000", "B", "b.com", ROW)

    @pytest.mark.asyncio
    async def test_fragmentation_guard_still_answers_when_healthy(self, monkeypatch):
        # Positive control both ways: conflict -> collision record, none -> None.
        import services.audit_index_intake as aii

        async def _conflict(*a, **k):
            return {"merchant_id": "merch_real", "product_key": "pk_other"}

        monkeypatch.setattr(aii, "_existing_brand_canonical_conflict", _conflict)
        bf = _backfill(_FakeDb())
        hit = await bf._fragmentation_collision("merch_obs_aaaa0000aaaa0000", "B", "b.com", ROW)
        assert hit and hit["conflict_merchant_id"] == "merch_real"

        async def _clear(*a, **k):
            return None

        monkeypatch.setattr(aii, "_existing_brand_canonical_conflict", _clear)
        assert await bf._fragmentation_collision(
            "merch_obs_aaaa0000aaaa0000", "B", "b.com", ROW) is None

    @pytest.mark.asyncio
    async def test_claimed_merchant_lookup_propagates_failure(self, monkeypatch):
        import services.seller_identity as si

        class _WedgedDb:
            async def fetch_all(self, *a, **k):
                raise AssertionError("Connection is already acquired")

        monkeypatch.setattr(si, "database", _WedgedDb())
        with pytest.raises(AssertionError, match="already acquired"):
            await si._resolve_claimed_merchant("claimed.com")

    @pytest.mark.asyncio
    async def test_claimed_merchant_still_answers_when_healthy(self, monkeypatch):
        import services.seller_identity as si

        class _Db:
            def __init__(self, rows):
                self.rows = rows

            async def fetch_all(self, *a, **k):
                return self.rows

        monkeypatch.setattr(si, "database", _Db(
            [{"merchant_id": "merch_tenant", "brand_domain": "www.claimed.com"}]))
        assert await si._resolve_claimed_merchant("claimed.com") == "merch_tenant"
        monkeypatch.setattr(si, "database", _Db([]))
        assert await si._resolve_claimed_merchant("claimed.com") is None


class TestRetryAfterReset:
    @pytest.mark.asyncio
    async def test_flake_gets_one_retry_on_a_fresh_connection(self):
        db = _FakeDb()
        bf = _backfill(db)
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError("proxy died mid-operation")
            return "ok"

        assert await bf._retry_after_reset(flaky, what="unit") == "ok"
        assert calls["n"] == 2
        assert db.resets == 1  # disconnect() then connect() between attempts

    @pytest.mark.asyncio
    async def test_second_failure_propagates_no_third_attempt(self):
        db = _FakeDb()
        bf = _backfill(db)
        calls = {"n": 0}

        async def broken():
            calls["n"] += 1
            raise RuntimeError("deterministic defect")

        with pytest.raises(RuntimeError, match="deterministic defect"):
            await bf._retry_after_reset(broken, what="unit")
        assert calls["n"] == 2
        assert db.resets == 1

    @pytest.mark.asyncio
    async def test_success_never_touches_the_connection(self):
        db = _FakeDb()
        bf = _backfill(db)

        async def fine():
            return 7

        assert await bf._retry_after_reset(fine, what="unit") == 7
        assert getattr(db, "resets", 0) == 0


class TestRetryWiring:
    """The review of PR #1716 found two mutants the suite missed: (a) call
    run_catalog's stages DIRECTLY (retry helper as dead code) and nothing
    fails; (b) re-add the review-queue extend inside _resubject_batch and
    nothing catches the double-extend. These drive run_catalog through a
    first-call flake at each stage and pin single-counted outcomes."""

    @pytest.mark.asyncio
    async def test_planning_flake_is_retried_through_run_catalog(self, monkeypatch):
        rig = dict(ROW, product_key="prod::external_seed::external_seed::rig1",
                   source_domain="jwx893-fz.myshopify.com", source_product_id="rig1")
        db = _FakeDb(products=[rig])
        bf = _backfill(db)
        real = bf._plan_one
        calls = {"n": 0}

        async def flaky_once(p):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError("proxy died mid-operation")
            return await real(p)

        monkeypatch.setattr(bf, "_plan_one", flaky_once)
        report = BackfillReport(mode="dry_run", started_at="t",
                                phases=["catalog"], batch_size=50)
        await bf.run_catalog(report)
        # A direct (unwrapped) call site would have crashed run_catalog here.
        assert calls["n"] == 2
        assert db.resets == 1
        # Single-counted despite the retry: one row, one review entry.
        assert report.catalog["scanned"] == 1
        assert report.catalog["review_excluded_rig"] == 1
        assert len(report.review_queue["catalog_unresolvable"]) == 1

    @pytest.mark.asyncio
    async def test_batch_flake_is_retried_and_skips_extend_exactly_once(self, monkeypatch):
        ulta = dict(ROW, product_key="prod::external_seed::external_seed::u1",
                    source_domain="ulta.com", source_product_id="u1")
        db = _FakeDb(products=[ulta])
        bf = _backfill(db)
        calls = {"n": 0}

        async def flaky_batch(batch, cascade):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError("socket died inside the transaction")
            return {"ok": True, "resubjected": len(batch),
                    "skipped_to_review": [{"product_key": "pk_skip"}]}

        monkeypatch.setattr(bf, "_resubject_batch", flaky_batch)
        report = BackfillReport(mode="dry_run", started_at="t",
                                phases=["catalog"], batch_size=50)
        await bf.run_catalog(report)
        assert calls["n"] == 2
        assert db.resets == 1
        assert report.catalog["batches"] == 1
        assert report.catalog["resubjected"] == 1
        # The caller extends off the RETURNED parity exactly once — a retried
        # batch (or an extend creeping back into _resubject_batch) would
        # produce two copies here.
        assert report.review_queue["catalog_execute_skips"] == [{"product_key": "pk_skip"}]
        assert len(report.parity) == 1


class TestGroupMembershipAllCandidateIds:
    """Verifier catch 2026-08-11 (pgm_banned_left=77 after the first 1,000):
    pgm rows key on seed external ids and foreign platforms too — the hunt
    must enumerate banned rows across ALL candidate ids on ANY platform,
    exactly the verifier's join, then move/retire each under its own
    (platform, pid). Third instance of the every-id class."""

    @pytest.mark.asyncio
    async def test_seed_keyed_row_on_a_foreign_platform_is_moved(self):
        db = _FakeDb(pgm_banned_rows=[
            {"platform": "amazon", "platform_product_id": "ext_seed_9",
             "product_group_id": "grp_banned"}])
        row = dict(ROW, listing_product_ids=["x1", "ext_seed_9"])
        stats = await _backfill(db)._resubject_group_membership(
            row, "merch_obs_aaaa0000aaaa0000")
        assert stats == {"moved": 1, "retired": 0}
        assert len(db.executed) == 1
        sql, params = db.executed[0]
        assert sql.startswith("UPDATE product_group_members SET merchant_id = :obs")
        # moved under ITS OWN key, not the catalog row's
        assert params["spid"] == "ext_seed_9"
        assert params["platform"] == "amazon"

    @pytest.mark.asyncio
    async def test_every_banned_row_is_handled_not_just_the_first(self):
        db = _FakeDb(pgm_target_exists=True, pgm_banned_rows=[
            {"platform": "external_seed", "platform_product_id": "x1",
             "product_group_id": "grp_banned"},
            {"platform": "amazon", "platform_product_id": "ext_seed_9",
             "product_group_id": "grp_banned"}])
        row = dict(ROW, listing_product_ids=["x1", "ext_seed_9"])
        stats = await _backfill(db)._resubject_group_membership(
            row, "merch_obs_aaaa0000aaaa0000")
        # observed rows already exist (pgm_target_exists) -> both retired
        assert stats == {"moved": 0, "retired": 2}
        assert all(sql.startswith("DELETE FROM product_group_members")
                   for sql, _ in db.executed)
        assert {p["spid"] for _, p in db.executed} == {"x1", "ext_seed_9"}

    @pytest.mark.asyncio
    async def test_no_banned_rows_means_no_writes(self):
        db = _FakeDb(pgm_banned_rows=[])
        stats = await _backfill(db)._resubject_group_membership(
            ROW, "merch_obs_aaaa0000aaaa0000")
        assert stats == {"moved": 0, "retired": 0}
        assert db.executed == []


class TestRepairPgmStragglers:
    @pytest.mark.asyncio
    async def test_execute_repair_moves_the_stranded_pgm_rows(self):
        db = _FakeDb(listing_exists=False, pgm_banned_rows=[
            {"platform": "amazon", "platform_product_id": "ext_seed_9",
             "product_group_id": "grp_banned"}])
        db.products = [ROW]

        async def fetch_all(sql, params=None, _orig=db.fetch_all):
            _assert_binds_match(sql, params)
            if "FROM a9_4_backfill_checkpoint ck" in sql:
                return [{"product_key": ROW["product_key"],
                         "observed_id": "merch_obs_aaaa0000aaaa0000",
                         "source_product_id": "x1",
                         "seed_listing_ids": ["ext_seed_9"]}]
            return await _orig(sql, params)

        db.fetch_all = fetch_all
        bf = SellerBackfill(database=db, si_mod=None, execute=True, batch_size=25)
        report = BackfillReport(mode="execute", started_at="t",
                                phases=["repair-listings"], batch_size=25)
        await bf.repair_listings(report)
        stats = report.catalog["listing_repair"]
        assert stats["pgm_stale_found"] == 1
        assert stats["pgm_moved"] == 1
        assert stats["pgm_retired"] == 0
        assert any(sql.startswith("UPDATE product_group_members") and
                   p["spid"] == "ext_seed_9" for sql, p in db.executed)

    @pytest.mark.asyncio
    async def test_dry_run_repair_counts_but_never_writes(self):
        db = _FakeDb(listing_exists=False, pgm_banned_rows=[
            {"platform": "amazon", "platform_product_id": "ext_seed_9",
             "product_group_id": "grp_banned"}])

        async def fetch_all(sql, params=None, _orig=db.fetch_all):
            _assert_binds_match(sql, params)
            if "FROM a9_4_backfill_checkpoint ck" in sql:
                return [{"product_key": ROW["product_key"],
                         "observed_id": "merch_obs_aaaa0000aaaa0000",
                         "source_product_id": "x1",
                         "seed_listing_ids": ["ext_seed_9"]}]
            return await _orig(sql, params)

        db.fetch_all = fetch_all
        bf = _backfill(db)  # execute=False
        report = BackfillReport(mode="dry_run", started_at="t",
                                phases=["repair-listings"], batch_size=25)
        await bf.repair_listings(report)
        stats = report.catalog["listing_repair"]
        assert stats["pgm_stale_found"] == 1
        assert stats["pgm_moved"] == 0
        assert db.executed == []
