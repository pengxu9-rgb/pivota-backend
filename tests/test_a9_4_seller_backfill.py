"""A9-4 (ADR-009 D4) — seller-of-record backfill tooling, unit tests.

Proves, without a database:
  - the read-only seller shadow (`resolve_seller_readonly`) yields the SAME
    (seller_ref, seed_kind) as the REAL A9-3 `services.seller_identity.
    derive_seed_seller` (no drift; dry-run predicts execute) — self / cross-mint
    / cross-claimed / no-destination / unresolvable-cross;
  - bare `attached_product_key` repair: happy (re-derives the mirror's storage
    key + verifies it resolves) and undecodable (routed to review, never a
    widened matcher / partial key — ADR-009 decision 3, IDENTITY_REFERENCE T1);
  - the SIG-FROZEN assertion (IDENTITY_REFERENCE T5) rejects any re-key SQL that
    references the write-once sig/canonical columns, and every generated re-key
    statement passes it;
  - the per-batch parity diff (row count + byte-identical sig set + servable set
    + cascade residue) fails LOUDLY on any drift;
  - resumability: `_checkpoint` is a no-op in dry-run and idempotent-upsert in
    execute; the phase scans are re-run-safe via their SQL guards;
  - ADR-008 fragmentation collision routes to review instead of merging.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_seller_of_record as bf  # noqa: E402
from services import seller_identity as si  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDB:
    """Minimal async DB stand-in. Routes queries by substring, like the A9-3
    _OwnDB fake. Records executes so tests can assert writes."""

    def __init__(
        self,
        *,
        claim_rows: Optional[List[Dict[str, Any]]] = None,
        existing_merchants: Optional[set] = None,
        anchor_source_ref: Optional[str] = None,
        catalog_product_keys: Optional[set] = None,
        seed_rows: Optional[List[Dict[str, Any]]] = None,
        source_id_index: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        self.claim_rows = claim_rows or []
        self.existing_merchants = existing_merchants or set()
        self.anchor_source_ref = anchor_source_ref
        self.catalog_product_keys = catalog_product_keys or set()
        self.seed_rows = seed_rows or []
        self.source_id_index = source_id_index or {}
        self.executed: List[tuple] = []

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None):
        q = " ".join(query.split())
        if "FROM brand_claims" in q:
            return list(self.claim_rows)
        if "FROM merchant_stores" in q:
            return []
        if "WHERE seller_ref IS NULL" in q or ("FROM external_product_seeds" in q and "NOT LIKE 'prod::%'" in q):
            return list(self.seed_rows)
        if "FROM catalog_products WHERE platform" in q or ("platform = :pf" in q):
            pid = (values or {}).get("pid")
            return [{"product_key": pk} for pk in self.source_id_index.get(pid, [])]
        return []

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        q = " ".join(query.split())
        if "FROM catalog_merchants WHERE merchant_id" in q:
            mid = (values or {}).get("mid")
            return {"merchant_id": mid} if mid in self.existing_merchants else None
        if "SELECT source_ref FROM catalog_merchants" in q:
            return {"source_ref": self.anchor_source_ref} if self.anchor_source_ref else None
        if "FROM merchant_onboarding" in q:
            return None
        if "FROM catalog_products WHERE product_key" in q:
            pk = (values or {}).get("pk")
            return {"product_key": pk} if pk in self.catalog_product_keys else None
        return None

    async def execute(self, query: str, values: Optional[Dict[str, Any]] = None):
        self.executed.append((" ".join(query.split()), values or {}))
        return None


# ---------------------------------------------------------------------------
# 1. Derivation reuse — readonly shadow == real derive_seed_seller
# ---------------------------------------------------------------------------

async def _both(monkeypatch, *, anchor, brand, dest, db: FakeDB, owns: bool):
    """Run the REAL derive_seed_seller and the script's readonly shadow against
    the same fake DB + primitives; return both (seller_ref, seed_kind)."""
    async def _owns(_anchor: str, _registrable: str) -> bool:
        return owns

    upserts: List[Dict[str, Any]] = []

    async def _noop_upsert(**kwargs: Any) -> None:
        upserts.append(kwargs)  # ensure_observed_seller's mint — recorded, not written

    monkeypatch.setattr(si, "database", db)
    monkeypatch.setattr(si, "_anchor_owns_domain", _owns)
    monkeypatch.setattr(si, "upsert_catalog_merchant", _noop_upsert)

    real = await si.derive_seed_seller(
        anchor_merchant_id=anchor, brand=brand, destination_domain=dest, source_system="t"
    )
    shadow = await bf.resolve_seller_readonly(
        si, anchor_merchant_id=anchor, brand=brand, destination_domain=dest
    )
    return real, (shadow.seller_ref, shadow.seed_kind), upserts


@pytest.mark.asyncio
async def test_readonly_equals_derive_self(monkeypatch):
    db = FakeDB(anchor_source_ref="anuko.com")
    real, shadow, _ = await _both(
        monkeypatch, anchor="merch_anchor", brand="Anuko",
        dest="https://anuko.com/p/1", db=db, owns=True,
    )
    assert real == ("merch_anchor", "self") == shadow


@pytest.mark.asyncio
async def test_readonly_equals_derive_cross_mint(monkeypatch):
    db = FakeDB()  # no claim, not existing -> would mint
    real, shadow, upserts = await _both(
        monkeypatch, anchor="merch_anchor", brand="OtherBrand",
        dest="otherbrand.com", db=db, owns=False,
    )
    expected_id = si.make_observed_merchant_id("OtherBrand", "otherbrand.com")
    assert real == (expected_id, "cross") == shadow
    # readonly did NOT write; the real path recorded a mint.
    assert len(upserts) == 1 and upserts[0]["merchant_id"] == expected_id


@pytest.mark.asyncio
async def test_readonly_equals_derive_cross_claimed(monkeypatch):
    # A verified brand_claim for the registrable domain wins (attach, never mint).
    db = FakeDB(claim_rows=[{"merchant_id": "merch_tenant", "brand_domain": "otherbrand.com"}])
    real, shadow, upserts = await _both(
        monkeypatch, anchor="merch_anchor", brand="OtherBrand",
        dest="otherbrand.com", db=db, owns=False,
    )
    assert real == ("merch_tenant", "cross") == shadow
    assert upserts == []  # claimed -> no mint


@pytest.mark.asyncio
async def test_readonly_equals_derive_no_destination(monkeypatch):
    db = FakeDB()
    real, shadow, _ = await _both(
        monkeypatch, anchor="merch_anchor", brand="Brand", dest=None, db=db, owns=False,
    )
    assert real == (None, None) == shadow


@pytest.mark.asyncio
async def test_readonly_equals_derive_unresolvable_cross(monkeypatch):
    # present destination but empty brand -> make_observed_merchant_id raises -> NULL
    db = FakeDB()
    real, shadow, _ = await _both(
        monkeypatch, anchor="merch_anchor", brand="", dest="brand.com", db=db, owns=False,
    )
    assert real == (None, None) == shadow


# ---------------------------------------------------------------------------
# 2. Bare-key repair
# ---------------------------------------------------------------------------

def test_candidate_storage_key_happy():
    key = bf.candidate_storage_key_from_seed({"external_product_id": "sku-9"})
    assert key == "prod::external_seed::external_seed::sku-9"


def test_candidate_storage_key_none_when_no_epid_or_too_long():
    assert bf.candidate_storage_key_from_seed({"external_product_id": ""}) is None
    assert bf.candidate_storage_key_from_seed({"external_product_id": "x" * 129}) is None


@pytest.mark.asyncio
async def test_barekey_repair_happy_and_undecodable():
    seeds = [
        # (a) decodable: derived key resolves to a real catalog_products row
        {"id": "s1", "external_product_id": "sku-1", "attached_product_key": "legacy-1",
         "canonical_url": None, "destination_url": None, "seed_data": {}},
        # (b) undecodable: no external_product_id, nothing to derive from
        {"id": "s2", "external_product_id": "", "attached_product_key": "legacy-2",
         "canonical_url": None, "destination_url": None, "seed_data": {}},
        # (c) decodable via (platform, source_product_id) unique look-up
        {"id": "s3", "external_product_id": "sku-3", "attached_product_key": "legacy-3",
         "canonical_url": None, "destination_url": None, "seed_data": {}},
    ]
    db = FakeDB(
        seed_rows=seeds,
        catalog_product_keys={"prod::external_seed::external_seed::sku-1"},
        source_id_index={"sku-3": ["prod::external_seed::external_seed::sku-3"]},
    )
    backfill = bf.SellerBackfill(database=db, si_mod=si, execute=True, batch_size=100)
    report = bf.BackfillReport(mode="execute", started_at="t", phases=["barekey"], batch_size=100)
    await backfill.run_barekey(report)

    assert report.barekey["scanned"] == 3
    assert report.barekey["repaired"] == 2
    assert report.barekey["undecodable"] == 1
    undecodable_ids = [r["seed_id"] for r in report.review_queue["barekey_undecodable"]]
    assert undecodable_ids == ["s2"]
    # Only the two decodable seeds were written, to their verified storage keys.
    writes = [(v.get("id"), v.get("pk")) for q, v in db.executed if "attached_product_key = :pk" in q]
    assert ("s1", "prod::external_seed::external_seed::sku-1") in writes
    assert ("s3", "prod::external_seed::external_seed::sku-3") in writes
    assert not any(i == "s2" for i, _ in writes)


# ---------------------------------------------------------------------------
# 3. SIG-FROZEN assertion (IDENTITY_REFERENCE T5 / ADR-009 D4.3)
# ---------------------------------------------------------------------------

def test_sig_frozen_rejects_sig_and_canonical_refs():
    for bad in (
        "UPDATE catalog_products SET pivota_signature_id = 'x'",
        "UPDATE catalog_products SET pivota_canonical_url = 'x'",
        "UPDATE t SET merchant_id=:o WHERE pivota_signature_id IS NOT NULL",
    ):
        with pytest.raises(AssertionError):
            bf.assert_sig_frozen_sql(bad)


def test_sig_frozen_allows_merchant_only_rekey():
    bf.assert_sig_frozen_sql(bf.CATALOG_PRODUCTS_RESUBJECT_SQL)  # must not raise
    # and every dependent shape the discovery loop can emit
    for scope in ("product_key", "content_key", "sku_key"):
        bf.assert_sig_frozen_sql(
            f"UPDATE beauty_x SET merchant_id = :obs WHERE {scope} = :scope AND merchant_id = :banned"
        )
    bf.assert_sig_frozen_sql(
        "UPDATE agent_pdp_view SET primary_merchant_id = :obs "
        "WHERE content_key = :scope AND primary_merchant_id = :banned"
    )


# ---------------------------------------------------------------------------
# 4. Per-batch parity diff
# ---------------------------------------------------------------------------

def _snap(rows, sigs, servable):
    return {"rows": rows, "sigs": sigs, "sig_count": len(sigs),
            "servable": set(servable), "servable_count": len(servable)}


def test_parity_ok_when_only_merchant_changes():
    backfill = bf.SellerBackfill(database=None, si_mod=si, execute=True, batch_size=100)
    before = _snap(2, {"pk1": ("sig_a", "u_a"), "pk2": ("sig_b", "u_b")}, {"pk1", "pk2"})
    after = _snap(2, {"pk1": ("sig_a", "u_a"), "pk2": ("sig_b", "u_b")}, {"pk1", "pk2"})
    diff = backfill._diff_parity(before, after, residue={})
    assert diff["ok"] and diff["sigs_frozen"] and diff["servable_ok"] and diff["rows_ok"]


def test_parity_fails_on_sig_change():
    backfill = bf.SellerBackfill(database=None, si_mod=si, execute=True, batch_size=100)
    before = _snap(1, {"pk1": ("sig_a", "u_a")}, {"pk1"})
    after = _snap(1, {"pk1": ("sig_DIFFERENT", "u_a")}, {"pk1"})
    diff = backfill._diff_parity(before, after, residue={})
    assert diff["ok"] is False and diff["sigs_frozen"] is False


def test_parity_fails_on_dropped_servable_or_cascade_residue():
    backfill = bf.SellerBackfill(database=None, si_mod=si, execute=True, batch_size=100)
    before = _snap(1, {"pk1": ("sig_a", "u_a")}, {"pk1"})
    after_darkened = _snap(1, {"pk1": ("sig_a", "u_a")}, set())  # sig no longer serves
    assert backfill._diff_parity(before, after_darkened, residue={})["ok"] is False
    # residue = a dependent table still holds the banned seller -> loud fail
    after_ok = _snap(1, {"pk1": ("sig_a", "u_a")}, {"pk1"})
    assert backfill._diff_parity(before, after_ok, residue={"beauty_shades": 3})["ok"] is False


# ---------------------------------------------------------------------------
# 5. Resumability / checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkpoint_noop_in_dry_run():
    db = FakeDB()
    backfill = bf.SellerBackfill(database=db, si_mod=si, execute=False, batch_size=100)
    await backfill.ensure_checkpoint_table()
    await backfill._checkpoint("catalog", "pk1", "merch_obs_x", "done")
    assert db.executed == []  # dry-run writes NOTHING (READ-ONLY)


@pytest.mark.asyncio
async def test_checkpoint_upserts_in_execute():
    db = FakeDB()
    backfill = bf.SellerBackfill(database=db, si_mod=si, execute=True, batch_size=100)
    await backfill.ensure_checkpoint_table()
    await backfill._checkpoint("catalog", "pk1", "merch_obs_x", "done")
    joined = " ".join(q for q, _ in db.executed)
    assert "CREATE TABLE IF NOT EXISTS a9_4_backfill_checkpoint" in joined
    assert "ON CONFLICT (phase, ref_id)" in joined  # idempotent re-run


@pytest.mark.asyncio
async def test_barekey_rerun_is_idempotent_via_sql_guard():
    # After repair, the seed's attached key starts with prod:: so the phase scan
    # (NOT LIKE 'prod::%') no longer returns it -> a re-run is a no-op.
    db = FakeDB(seed_rows=[], catalog_product_keys=set())
    backfill = bf.SellerBackfill(database=db, si_mod=si, execute=True, batch_size=100)
    report = bf.BackfillReport(mode="execute", started_at="t", phases=["barekey"], batch_size=100)
    await backfill.run_barekey(report)
    assert report.barekey["scanned"] == 0 and report.barekey["repaired"] == 0


# ---------------------------------------------------------------------------
# 6. ADR-008 fragmentation collision routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collision_routes_to_review(monkeypatch):
    backfill = bf.SellerBackfill(database=FakeDB(), si_mod=si, execute=False, batch_size=100)

    async def _conflict(_mid: str, _fields: Dict[str, Any]):
        return {"merchant_id": "merch_REAL_OTHER", "product_key": "prod::merch_REAL_OTHER::shopify::9"}

    monkeypatch.setattr(
        "services.audit_index_intake._existing_brand_canonical_conflict", _conflict
    )
    collision = await backfill._fragmentation_collision(
        "merch_obs_abc", "Brand", "brand.com",
        {"product_key": "prod::external_seed::external_seed::x", "canonical_url": "https://brand.com/x"},
    )
    assert collision is not None
    assert collision["conflict_merchant_id"] == "merch_REAL_OTHER"


@pytest.mark.asyncio
async def test_collision_ignores_sibling_bucket_or_same_identity(monkeypatch):
    backfill = bf.SellerBackfill(database=FakeDB(), si_mod=si, execute=False, batch_size=100)

    async def _sibling(_mid: str, _fields: Dict[str, Any]):
        return {"merchant_id": bf.BANNED_BUCKET_MERCHANT_ID, "product_key": "prod::external_seed::external_seed::y"}

    monkeypatch.setattr(
        "services.audit_index_intake._existing_brand_canonical_conflict", _sibling
    )
    # A same-bucket sibling collapses to the same observed seller — not a collision.
    assert await backfill._fragmentation_collision(
        "merch_obs_abc", "Brand", "brand.com",
        {"product_key": "prod::external_seed::external_seed::x", "canonical_url": None},
    ) is None


# ---------------------------------------------------------------------------
# _seed_brand — the snapshot shape (the silent zero-write bug)
# ---------------------------------------------------------------------------


def test_seed_brand_reads_the_snapshot_shape():
    """THE REGRESSION TEST.

    A9-4 phase 1 ran to completion in --execute mode on prod 2026-07-31 and
    wrote ZERO of 2,026 rows. Cause: every one of those seeds carries its brand
    at seed_data.snapshot.brand, and this resolver only looked at
    seed_row.brand / seed_data.brand / seed_data.vendor. With an empty brand,
    resolve_seller_readonly refuses — and under the no-fallback rule a missing
    identity is DEBUG-quiet, so it failed silently.

    Measured over the 2,026: brand at top level 0, brand ONLY under snapshot
    2,024, vendor at top level 0, genuinely no brand 2.
    """
    from scripts.backfill_seller_of_record import _seed_brand

    row = {"domain": None, "destination_url": "https://beautyofjoseon.com/products/x"}
    seed_data = {"snapshot": {"brand": "Beauty of Joseon"}}
    assert _seed_brand(row, seed_data) == "Beauty of Joseon"


def test_seed_brand_resolution_order_matches_the_sibling_resolvers():
    """snapshot -> row -> seed_data, the order
    index_pipeline_state_service._resolve_seed_title uses (itself matching
    external_seed_audit.audit_external_seed_row). A sibling resolver that
    disagrees on precedence is how these shapes drift apart."""
    from scripts.backfill_seller_of_record import _seed_brand

    assert _seed_brand({"brand": "RowBrand"},
                       {"snapshot": {"brand": "SnapBrand"}, "brand": "DataBrand"}) == "SnapBrand"
    assert _seed_brand({"brand": "RowBrand"}, {"brand": "DataBrand"}) == "RowBrand"
    assert _seed_brand({}, {"brand": "DataBrand", "vendor": "Vendor"}) == "DataBrand"
    assert _seed_brand({}, {"vendor": "Vendor"}) == "Vendor"


def test_seed_brand_still_returns_empty_when_there_is_genuinely_no_brand():
    """The no-fallback rule depends on this. 2 of the 2,026 have no brand
    anywhere, and they must stay unresolvable rather than acquire an invented
    seller identity."""
    from scripts.backfill_seller_of_record import _seed_brand

    assert _seed_brand({}, {}) == ""
    assert _seed_brand({"brand": "  "}, {"snapshot": {"brand": ""}}) == ""
    assert _seed_brand({}, {"snapshot": {}}) == ""


def test_seed_brand_tolerates_a_non_dict_snapshot():
    """seed_data.snapshot is JSON from many writers; a string or list there must
    not raise inside a backfill that is mid-scan over 2,026 rows."""
    from scripts.backfill_seller_of_record import _seed_brand

    assert _seed_brand({}, {"snapshot": "not-a-dict", "brand": "Fallback"}) == "Fallback"
    assert _seed_brand({}, {"snapshot": ["also", "not"], "brand": "Fallback"}) == "Fallback"


def test_the_prod_shape_now_resolves_end_to_end():
    """Brand + destination together, on the real refused row shape: the
    destination was never the problem (_seed_destination already falls back to
    destination_url and etld1 reduces it), only the brand was."""
    from scripts.backfill_seller_of_record import _seed_brand, _seed_destination
    from services.seller_identity import etld1

    row = {"domain": None,
           "destination_url": "https://beautyofjoseon.com/products/glow-deep-serum-",
           "canonical_url": None}
    seed_data = {"snapshot": {"brand": "Beauty of Joseon"}}

    assert _seed_brand(row, seed_data) == "Beauty of Joseon"
    assert etld1(_seed_destination(row, seed_data)) == "beautyofjoseon.com"
