from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.mirror_external_seeds_to_catalog_products import (  # noqa: E402
    CATEGORY_CONFIDENCE_REGEX_AT_MIRROR,
    CATEGORY_LABEL_SOURCE_AT_MIRROR,
    COMMON_CTES,
    MERCHANT_ID,
    OFFER_ID_PREFIX,
    PLATFORM,
    READINESS_TIER,
    SKU_SUFFIX,
    SOURCE_SYSTEM,
    _compute_mirror_lifecycle_stage,
    _derive_mirror_offer_id,
    _derive_mirror_sku_key,
    _ensure_external_seed_merchant,
    _extract_tags_from_seed_data,
    _upsert_canonical_offer_for_mirror_row,
    _upsert_canonical_sku_for_mirror_row,
    resolve_mirror_category_metadata,
)
import scripts.mirror_external_seeds_to_catalog_products as mirror_module  # noqa: E402
# P1.6: the offer upsert delegates to the shared projection module, so the offer
# tests patch the database there (the sku/merchant writes stay in mirror_module).
import services.external_offer_dual_write as dual_write_module  # noqa: E402


def test_mirror_query_includes_attached_seed_rows() -> None:
    """Attached seeds are review-gated seller edges, so the mirror must
    create their catalog product/offer chain too."""
    assert "active_attached AS" in COMMON_CTES
    assert "active_mirrorable AS" in COMMON_CTES
    assert "FROM active_mirrorable eps" in COMMON_CTES


def test_mirror_missing_treats_attached_existing_canonical_as_present() -> None:
    """A seed whose attached_product_key resolves to an existing catalog row must
    NOT be re-mirrored: the enrichment agent mints pdp.source_product_id (slug)
    and seed.external_product_id (brand:hash) in different formats, so the
    (platform, source_product_id) join alone misses that mirror — every Path-C
    ingest spawned a merch_obs_* shadow product on the next tick (39 COSRX
    shadows, 2026-07-16). The check is GROUP-level (NOT EXISTS over active_all
    by external_product_id), not just the ranked rn=1 winner — mixed
    attached/unattached duplicate groups would otherwise mint the shadow via
    the unattached winner. Self-heal preserved: if every attached target row
    is gone, the group is mirrorable again.

    THIS IS STILL A SPELLING TEST, and saying otherwise would repeat the
    mistake it exists to document. A substring assert cannot tell
    `SELECT DISTINCT a.external_product_id` from an equivalent `GROUP BY`, and
    renaming the alias `ae` would fail it for a correct change. It is kept only
    as a cheap smoke check that the attachment logic was not deleted outright;
    the BEHAVIOUR is owned by
    tests/test_missing_mirror_count_equivalence_postgres.py, which executes both
    chains on real Postgres and now also executes `candidates_attached_present`
    against a group with two attached seeds — so dropping the DISTINCT fails a
    real query rather than a string match. If you are rewriting this SQL and one
    of these asserts blocks you, change the assert; trust the Postgres gate.

    What this replaced, and why. They used to require
    the literal `a.external_product_id = c.external_product_id`, i.e. that the
    check be written as a CORRELATED subquery. That correlation was the
    2026-08-20 outage: `active_all` is a materialised CTE, materialised CTEs
    carry no statistics, the planner estimated 63 rows against 11,352, chose a
    Nested Loop and re-scanned the CTE once per candidate — 64,508,866 inner
    iterations, 72.7s per call, ~2,600 calls, which exhausted the 20-slot pool
    and starved HTTP. The check is now a pre-computed `attached_epids` set and
    an anti-join: same rows, 160x faster. A test that pins one implementation
    of a property blocks the fix for a defect in that implementation.

    The BEHAVIOUR — that this set matches the cheap chain's, including the
    duplicate/mixed-attachment groups production does not currently contain —
    is proven by tests/test_missing_mirror_count_equivalence_postgres.py.
    Verified 2026-08-20: dropping the anti-join condition fails 2 of its tests,
    and dropping the DISTINCT fails the attached-present counter."""
    # the attachment join itself
    assert "cp_attached.product_key = a.attached_product_key" in COMMON_CTES
    # keyed on the GROUP (external_product_id), not the rn=1 winner
    assert "SELECT DISTINCT a.external_product_id" in COMMON_CTES
    # and candidates whose group is attached are excluded
    assert "ae.external_product_id IS NULL" in COMMON_CTES


def test_mirror_insert_mints_canonical_signature_on_new_rows() -> None:
    """New Path B catalog mirror rows should be addressable by sig_* as soon
    as they are created. Public serving is still controlled elsewhere."""
    source = Path(mirror_module.__file__).read_text()

    # imported from catalog_sync_service (grouped import since A9-2 added
    # make_catalog_product_key alongside it)
    assert "make_pivota_canonical_fields" in source
    assert "pivota_fields = make_pivota_canonical_fields(" in source
    assert "pivota_signature_id" in source
    assert "pivota_canonical_url" in source
    assert "pivota_signature_minted_at" in source
    assert "ON CONFLICT (merchant_id, platform, source_product_id) DO NOTHING" in source


def test_mirror_insert_classifies_category_path() -> None:
    # A lip gloss resolves to the Lip Gloss subtype, not the Lipstick catch-all:
    # the classifier grew dedicated lip subtype patterns ahead of Lipstick.
    meta = resolve_mirror_category_metadata(
        category=None,
        product_type="Lip Gloss",
        title="Fenty Beauty Gloss Bomb Lip Gloss",
    )

    assert meta["category_path"] == "beauty/makeup/lip/gloss"
    assert meta["category_confidence"] == CATEGORY_CONFIDENCE_REGEX_AT_MIRROR
    assert meta["category_label_source"] == CATEGORY_LABEL_SOURCE_AT_MIRROR
    assert meta["category_label"] == "Lip Gloss"


def test_mirror_insert_leaves_unknown_category_null() -> None:
    meta = resolve_mirror_category_metadata(
        category=None,
        product_type="Accessory",
        title="Travel Makeup Bag",
    )

    assert meta["category_path"] is None
    assert meta["category_confidence"] is None
    assert meta["category_label_source"] is None
    assert meta["category_label"] is None


# ---------------------------------------------------------------------------
# Phase O-1 followup — tag extraction from seed_data JSONB
# ---------------------------------------------------------------------------


def test_extract_tags_from_top_level_list() -> None:
    """Most basic case: scraper put a flat list at seed_data.tags."""
    out = _extract_tags_from_seed_data({"tags": ["matte", "long-wear"]})
    assert out == ["matte", "long-wear"]


def test_extract_tags_from_snapshot_path() -> None:
    """Shopify-style scrape: tags live under seed_data.snapshot.tags."""
    out = _extract_tags_from_seed_data(
        {"snapshot": {"tags": ["vegan", "cruelty-free", "k-beauty"]}}
    )
    assert out == ["vegan", "cruelty-free", "k-beauty"]


def test_extract_tags_from_derived_recall_path() -> None:
    """Pivota-derived recall doc — preferred path; takes priority over
    snapshot/product/top-level."""
    out = _extract_tags_from_seed_data(
        {
            "derived": {"recall": {"tags": ["serum", "ceramide"]}},
            "snapshot": {"tags": ["should-be-ignored"]},
            "tags": ["also-ignored"],
        }
    )
    assert out == ["serum", "ceramide"]


def test_extract_tags_handles_comma_separated_string() -> None:
    """Some Shopify exports flatten tags into a single comma string."""
    out = _extract_tags_from_seed_data({"tags": "matte, long-wear, vegan"})
    assert out == ["matte", "long-wear", "vegan"]


def test_extract_tags_handles_dict_items() -> None:
    """Some scrapers wrap each tag as {name: ...} or {label: ...}."""
    out = _extract_tags_from_seed_data(
        {"tags": [{"name": "matte"}, {"label": "long-wear"}, {"name": ""}, "vegan"]}
    )
    assert out == ["matte", "long-wear", "vegan"]


def test_extract_tags_dedupes_and_strips() -> None:
    out = _extract_tags_from_seed_data(
        {"tags": ["  matte  ", "matte", "long-wear", "", "long-wear"]}
    )
    assert out == ["matte", "long-wear"]


def test_extract_tags_returns_empty_when_no_tag_field() -> None:
    """Same semantic as ingest_standard_products on Path A: empty list,
    not None — meaning "we looked, found nothing"."""
    out = _extract_tags_from_seed_data({"title": "Just a product"})
    assert out == []


def test_extract_tags_returns_empty_for_non_dict() -> None:
    """seed_data may be NULL or non-dict on legacy rows — must not blow up."""
    assert _extract_tags_from_seed_data(None) == []
    assert _extract_tags_from_seed_data([]) == []
    assert _extract_tags_from_seed_data("scrambled string") == []


# ---------------------------------------------------------------------------
# Phase O-4 — lifecycle stage computation on Path B (external seed mirror)
# ---------------------------------------------------------------------------


def test_mirror_lifecycle_validated_when_full_content_and_taxonomy() -> None:
    """Path B reaches validated when title + image + long description
    + category_path + at least one taxonomy signal are all present.
    Path B never reaches published — pdp_scope is NULL in this script."""
    row = {
        "title": "Hydrating Vitamin C Serum",
        "mirrored_description": "Daily-use vegan serum with niacinamide for brightening and hydration support.",
        "image_url": "https://example.com/serum.jpg",
    }
    category_meta = {"category_path": "beauty/skincare/serum"}
    taxonomy = {
        "demographic": "women",
        "use_case_tags": ["daily"],
        "lifestyle_tags": ["vegan"],
    }
    stage = _compute_mirror_lifecycle_stage(
        row, category_meta, ["k-beauty"], taxonomy
    )
    assert stage == "validated"


def test_mirror_lifecycle_candidate_when_no_category_path() -> None:
    """Without category_path the row caps at candidate — same gate as
    the lifecycle module, applied via the script's helper."""
    row = {
        "title": "A Good Product",
        "mirrored_description": "A long enough description to pass the candidate description gate.",
        "image_url": "https://example.com/x.jpg",
    }
    category_meta = {"category_path": None}
    taxonomy = {"demographic": None, "use_case_tags": [], "lifestyle_tags": []}
    stage = _compute_mirror_lifecycle_stage(row, category_meta, [], taxonomy)
    assert stage == "candidate"


def test_mirror_lifecycle_draft_for_thin_content() -> None:
    """Empty seed → draft. Mirror script must still write the column,
    not skip it."""
    row = {"title": None, "mirrored_description": None, "image_url": None}
    category_meta = {"category_path": None}
    taxonomy = {"demographic": None, "use_case_tags": [], "lifestyle_tags": []}
    stage = _compute_mirror_lifecycle_stage(row, category_meta, [], taxonomy)
    assert stage == "draft"


# ---------------------------------------------------------------------------
# Phase 7d — full canonical chain (catalog_skus + catalog_offers)
# ---------------------------------------------------------------------------


def test_mirror_sku_key_appends_canonical_suffix() -> None:
    """Path B sku_key must match Path C's `<product_key>::canonical`
    convention so the chain reads identically across paths."""
    pk = "prod::external_seed::external_seed::ext_abc123"
    sku_key = _derive_mirror_sku_key(pk)
    assert sku_key == f"{pk}::canonical"
    assert sku_key.endswith(SKU_SUFFIX)


def test_mirror_offer_id_is_deterministic_and_prefixed() -> None:
    """Same product_key → same offer_id. Different product_key → different.
    Pin the prefix so the offer_id is recognizable in audit trails as a
    Path-B-mirror-origin offer."""
    pk_a = "prod::external_seed::external_seed::ext_abc"
    pk_b = "prod::external_seed::external_seed::ext_def"
    a1 = _derive_mirror_offer_id(pk_a)
    a2 = _derive_mirror_offer_id(pk_a)
    b = _derive_mirror_offer_id(pk_b)
    assert a1 == a2
    assert a1 != b
    assert a1.startswith(OFFER_ID_PREFIX)


def test_mirror_offer_id_under_64_chars() -> None:
    """catalog_offers.offer_id is VARCHAR(64). Path B uses a 32-char
    sha256 truncation + the 'offer:external_seed:' prefix (20 chars) =
    52 chars total. Pin so a future prefix change can't blow the limit."""
    offer_id = _derive_mirror_offer_id("prod::" + "x" * 200)
    assert len(offer_id) <= 64


@pytest.mark.asyncio
async def test_ensure_external_seed_merchant_upserts_singleton(monkeypatch) -> None:
    """The synthetic 'external_seed' merchant must exist before any
    catalog_offers.foreign_key=merchant_id resolves. _apply calls this
    once per run; the SQL must be idempotent (ON CONFLICT DO UPDATE)."""
    executed: list = []

    class DummyDB:
        async def execute(self, sql, params):
            executed.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(mirror_module, "database", DummyDB())

    await _ensure_external_seed_merchant()

    assert len(executed) == 1
    sql = executed[0]["sql"]
    params = executed[0]["params"]
    assert "INSERT INTO catalog_merchants" in sql
    assert "ON CONFLICT (merchant_id) DO UPDATE" in sql
    assert params["merchant_id"] == MERCHANT_ID
    assert params["primary_platform"] == PLATFORM
    assert params["source_system"] == SOURCE_SYSTEM


@pytest.mark.asyncio
async def test_upsert_canonical_sku_writes_path_C_compatible_shape(monkeypatch) -> None:
    """Sku row from Path B must be readable by the same downstream
    consumers as Path C — same sku_key convention, same merchant/platform
    fields, same JSONB column shapes (jsonb-cast strings)."""
    executed: list = []

    class DummyDB:
        async def execute(self, sql, params):
            executed.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(mirror_module, "database", DummyDB())

    pk = "prod::merch_obs_abc123::external_seed::ext_abc"
    row_dict = {
        "external_product_id": "ext_abc",
        "title": "Test Product",
        "image_url": "https://example.com/img.jpg",
        "price_currency": "USD",
        "destination_url": "https://example.com/p/x",
    }
    await _upsert_canonical_sku_for_mirror_row(pk, row_dict)

    assert len(executed) == 1
    sql = executed[0]["sql"]
    params = executed[0]["params"]
    assert "INSERT INTO catalog_skus" in sql
    assert "ON CONFLICT (sku_key) DO UPDATE" in sql
    assert params["sku_key"] == f"{pk}::canonical"
    assert params["product_key"] == pk
    assert params["merchant_id"] == MERCHANT_ID
    assert params["platform"] == PLATFORM
    assert params["source_product_id"] == "ext_abc"
    # Phase 7d fix: source_variant_id = product_key (NOT literal
    # 'canonical'). The unique index `idx_catalog_skus_source_identity`
    # is on (merchant_id, platform, source_variant_id) only 3 columns —
    # so a literal would collide on every row past the first. Pin the
    # product_key convention (matches Path C agent) so a future refactor
    # can't accidentally re-introduce the collision.
    assert params["source_variant_id"] == pk
    assert params["title"] == "Test Product"
    assert params["currency"] == "USD"
    assert params["readiness_tier"] == READINESS_TIER
    # JSONB columns must be JSON-stringified for CAST(:... AS jsonb)
    assert isinstance(params["visible_attributes"], str)
    assert isinstance(params["sku_payload"], str)


@pytest.mark.asyncio
async def test_upsert_canonical_offer_carries_price_amount_into_three_columns(monkeypatch) -> None:
    """The whole point of Phase 7d: external_product_seeds.price_amount
    must reach catalog_offers as list_price + merchant_effective_price
    + estimated_best_price so downstream JOINs surface a price for
    Path B rows. Pin the 1:1 mapping."""
    executed: list = []

    class DummyDB:
        async def execute(self, sql, params):
            executed.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(dual_write_module, "database", DummyDB())

    pk = "prod::merch_obs_abc123::external_seed::ext_abc"
    row_dict = {
        "id": "eps_123",
        "external_product_id": "ext_abc",
        "price_amount": 28.5,
        "price_currency": "USD",
        "availability": "in_stock",
        "destination_url": "https://example.com/p/x",
        "canonical_url": "https://example.com/p/x",
        "domain": "example.com",
        "market": "US",
    }
    await _upsert_canonical_offer_for_mirror_row(pk, row_dict, merchant_id="merch_obs_abc123")

    assert len(executed) == 1
    sql = executed[0]["sql"]
    params = executed[0]["params"]
    assert "INSERT INTO catalog_offers" in sql
    assert "ON CONFLICT (offer_id) DO UPDATE" in sql
    # Price flows into all three pricing columns (single-source seed).
    assert params["list_price"] == 28.5
    assert params["merchant_effective_price"] == 28.5
    assert params["estimated_best_price"] == 28.5
    assert params["currency"] == "USD"
    assert params["sku_key"] == f"{pk}::canonical"
    assert params["product_key"] == pk
    # Offer attaches under the seed's REAL per-brand observed seller (ADR-009 D2),
    # never the 'external_seed' sentinel.
    assert params["merchant_id"] == "merch_obs_abc123"
    assert params["merchant_id"] != MERCHANT_ID
    assert params["catalog_track"] == "external_referral"
    assert params["offer_mode"] == "redirect"
    assert params["availability"] == "in_stock"
    assert params["source_ref"] == "eps_123"
    # offer_id deterministic from product_key + recognizable prefix
    assert params["offer_id"].startswith(OFFER_ID_PREFIX)


@pytest.mark.asyncio
async def test_upsert_canonical_offer_handles_null_price_gracefully(monkeypatch) -> None:
    """If price_amount is NULL on the seed (some scrapers don't extract
    it), the offer still writes — list_price stays NULL, price_confidence
    is NULL, and the row appears in catalog_offers so the downstream
    JOIN doesn't drop the product entirely. Better to surface "price
    unavailable" than to hide the row."""
    executed: list = []

    class DummyDB:
        async def execute(self, sql, params):
            executed.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(dual_write_module, "database", DummyDB())

    pk = "prod::external_seed::external_seed::ext_xyz"
    row_dict = {
        "id": "eps_999",
        "external_product_id": "ext_xyz",
        "price_amount": None,
        "price_currency": None,
        "availability": "in_stock",
        "destination_url": "https://example.com/p/y",
        "canonical_url": None,
        "domain": "example.com",
        "market": "US",
    }
    await _upsert_canonical_offer_for_mirror_row(pk, row_dict, merchant_id="merch_obs_abc123")

    params = executed[0]["params"]
    assert params["list_price"] is None
    assert params["merchant_effective_price"] is None
    assert params["estimated_best_price"] is None
    # Confidence must be NULL when price is — surfacing 0.6 confidence
    # on a missing-price row would be a lie.
    assert params["price_confidence"] is None
    # Default currency falls back to USD so JOIN consumers don't choke
    # on NULL string handling.
    assert params["currency"] == "USD"
