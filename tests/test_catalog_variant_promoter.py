"""Tests for services/catalog_variant_promoter.py — Stage 2b-ii.

Anchored to the real-world data shapes confirmed on 2026-05-12:
  - Tom Ford (Path B): seed_data.snapshot.variants[] with 40 entries,
    each carrying shade + size options under structured `options` lists.
  - MOYU (Path A): product_payload.variants[] with 1 entry, title
    "Default Title", options {"Title": "Default Title"} — Shopify
    single-variant placeholder, should be filtered out.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import catalog_variant_promoter as promoter  # noqa: E402
from services.catalog_variant_promoter import (  # noqa: E402
    GroupOutcome,
    VariantRow,
    _derive_sku_key,
    _extract_variants_from_payload,
    _extract_variants_from_seed,
    _variant_options_are_meaningful,
    build_variant_row,
    filter_real_variants,
    is_real_variant,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tomford_variant(variant_id: str = "53059916267733") -> Dict[str, Any]:
    """One Tom Ford shade variant — exact shape from prod 2026-05-12."""
    return {
        "sku": "TCT117",
        "price": "95.00",
        "stock": "In Stock",
        "title": "8.5N Vellum / 30.0 ml",
        "options": [
            {"name": "Shade", "value": "8.5N Vellum", "axis_kind": "shade"},
            {"name": "Size", "value": "30.0 ml", "axis_kind": "size"},
        ],
        "currency": "USD",
        "image_url": "https://cdn.shopify.com/.../tf_sku_TCT117_3000x3000_0.png",
        "variant_id": variant_id,
        "barcode": None,
    }


def _moyu_default_variant() -> Dict[str, Any]:
    """One Shopify Default-Title placeholder — MOYU 'Foundation Brush'
    shape."""
    return {
        "id": "53012671693097",
        "variant_id": "53012671693097",
        "sku": "moyu-brush-foundation",
        "barcode": None,
        "title": "Default Title",
        "options": {"Title": "Default Title"},
        "image_url": None,
        "currency": "USD",
        "inventory_quantity": 1198,
    }


# ---------------------------------------------------------------------------
# _extract_variants_from_seed
# ---------------------------------------------------------------------------


def test_extract_seed_returns_variants_array() -> None:
    seed = {"snapshot": {"variants": [_tomford_variant("1"), _tomford_variant("2")]}}
    out = _extract_variants_from_seed(seed)
    assert len(out) == 2
    assert out[0]["variant_id"] == "1"


def test_extract_seed_handles_jsonb_string_coercion() -> None:
    """asyncpg sometimes returns jsonb columns as JSON strings.
    Defensive coerce — don't crash on either shape."""
    seed_str = json.dumps({"snapshot": {"variants": [_tomford_variant("x")]}})
    out = _extract_variants_from_seed(seed_str)
    assert len(out) == 1


def test_extract_seed_returns_empty_for_missing_snapshot() -> None:
    """seed_data without a snapshot key → empty list, no crash."""
    assert _extract_variants_from_seed({}) == []
    assert _extract_variants_from_seed({"snapshot": {}}) == []
    assert _extract_variants_from_seed({"snapshot": {"variants": None}}) == []
    assert _extract_variants_from_seed(None) == []
    assert _extract_variants_from_seed("garbage") == []


# ---------------------------------------------------------------------------
# _extract_variants_from_payload (Path A)
# ---------------------------------------------------------------------------


def test_extract_payload_returns_variants_array() -> None:
    payload = {"variants": [_moyu_default_variant()]}
    out = _extract_variants_from_payload(payload)
    assert len(out) == 1
    assert out[0]["title"] == "Default Title"


def test_extract_payload_returns_empty_when_no_variants_key() -> None:
    assert _extract_variants_from_payload({}) == []
    assert _extract_variants_from_payload({"variants": "not_a_list"}) == []
    assert _extract_variants_from_payload(None) == []


# ---------------------------------------------------------------------------
# Real-variant filter — the critical line between Tom Ford and MOYU
# ---------------------------------------------------------------------------


def test_is_real_variant_accepts_tomford_shade() -> None:
    """Tom Ford's 8.5N Vellum: structured options + meaningful title."""
    assert is_real_variant(_tomford_variant()) is True


def test_is_real_variant_rejects_moyu_default_placeholder() -> None:
    """MOYU's single-variant Shopify default: title 'Default Title',
    options {'Title': 'Default Title'}. Must be filtered out — agent
    UI shouldn't render a shade selector with one swatch labeled
    'Default Title'."""
    assert is_real_variant(_moyu_default_variant()) is False


def test_is_real_variant_accepts_title_only_when_options_default() -> None:
    """Some real variants have a meaningful title but no options dict
    (legacy scrapes). Title-only is enough to qualify."""
    v = {"variant_id": "x", "title": "30ml", "options": {"Title": "Default Title"}}
    assert is_real_variant(v) is True


def test_is_real_variant_accepts_meaningful_options_even_with_default_title() -> None:
    """Conversely, options carry real values even when title is
    'Default Title' (rare but seen on some scrapes)."""
    v = {
        "variant_id": "x", "title": "Default Title",
        "options": [{"name": "Color", "value": "Red", "axis_kind": "color"}],
    }
    assert is_real_variant(v) is True


def test_is_real_variant_handles_path_a_options_dict_shape() -> None:
    """Path A serializes options as a flat dict {"Color": "Red"}.
    Path B uses a list of dicts. Both must work."""
    path_a = {"variant_id": "x", "title": "Red 30ml", "options": {"Color": "Red", "Size": "30ml"}}
    assert is_real_variant(path_a) is True
    path_a_default = {"variant_id": "x", "title": "Default Title", "options": {"Title": "Default Title"}}
    assert is_real_variant(path_a_default) is False


def test_filter_real_variants_drops_only_defaults() -> None:
    """Mixed array → only the real ones survive."""
    arr = [
        _tomford_variant("real_1"),
        _moyu_default_variant(),  # default
        _tomford_variant("real_2"),
    ]
    out = filter_real_variants(arr)
    assert len(out) == 2
    assert {v["variant_id"] for v in out} == {"real_1", "real_2"}


# ---------------------------------------------------------------------------
# build_variant_row — the catalog_skus upsert payload
# ---------------------------------------------------------------------------


def _primary() -> Dict[str, Any]:
    return {
        "product_key": "prod::external_seed::external_seed::ext_pk_x",
        "merchant_id": "external_seed",
        "platform": "external_seed",
        "source_product_id": "ext_pk_x",
        "parent_title": "Architecture Radiance Hydrating Foundation",
    }


def test_build_variant_row_sku_key_is_deterministic() -> None:
    """sku_key = <primary_product_key>::v::<variant_id>. Idempotent —
    re-running the promoter on the same input produces the same key.
    Distinct from the existing `::canonical` synthetic SKU so they
    coexist on catalog_skus PK."""
    row = build_variant_row(variant=_tomford_variant("V1"), primary=_primary())
    assert row.sku_key == "prod::external_seed::external_seed::ext_pk_x::v::V1"
    # `::canonical` doesn't collide
    canonical = _primary()["product_key"] + "::canonical"
    assert row.sku_key != canonical


def test_build_variant_row_returns_none_when_no_variant_id() -> None:
    """No variant_id → can't compute a stable sku_key. Skip rather
    than synthesize a key that would break dedup on re-runs."""
    v = {"sku": "x", "title": "X"}  # no variant_id, no id
    assert build_variant_row(variant=v, primary=_primary()) is None


def test_build_variant_row_visible_option_labels_path_b() -> None:
    """Path B options (list of {name, value, axis_kind}) flatten to
    a list of value strings — used by the agent UI's swatch labels."""
    row = build_variant_row(variant=_tomford_variant(), primary=_primary())
    assert "8.5N Vellum" in row.visible_option_labels
    assert "30.0 ml" in row.visible_option_labels


def test_build_variant_row_visible_attributes_uses_axis_kind() -> None:
    """Path B's axis_kind ('shade', 'size') is more semantic than
    'name' ('Shade', 'Size'). Pin axis_kind preferred."""
    row = build_variant_row(variant=_tomford_variant(), primary=_primary())
    assert row.visible_attributes.get("shade") == "8.5N Vellum"
    assert row.visible_attributes.get("size") == "30.0 ml"


def test_build_variant_row_falls_back_to_id_when_variant_id_missing() -> None:
    """Path A's StandardProduct sometimes has only 'id', not
    'variant_id'. Both must work for sku_key derivation."""
    v = {"id": "53012671693097", "title": "Red", "options": {"Color": "Red"}}
    row = build_variant_row(variant=v, primary=_primary())
    assert row is not None
    assert row.source_variant_id == "53012671693097"


def test_build_variant_row_provides_safe_defaults_for_missing_fields() -> None:
    """A variant missing barcode, image, currency, sku still produces
    a valid VariantRow (catalog_skus columns allow NULL on those)."""
    v = {"variant_id": "x", "title": "Red", "options": {"Color": "Red"}}
    row = build_variant_row(variant=v, primary=_primary())
    assert row.title == "Red"
    assert row.barcode is None
    assert row.image_url is None
    assert row.currency is None


# ---------------------------------------------------------------------------
# Async orchestration — promote_variants_for_group
# ---------------------------------------------------------------------------


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _install_fake_db(monkeypatch, catalog_track=None) -> List[Dict[str, Any]]:
    """`catalog_track` is EXPLICIT per test because two different things now read
    it: `promoted_readiness_tier` decides the tier offered, and the group outcome
    splits `variants_promoted` from `variants_tier_held` on whether that tier can
    move at all. The DEFAULT is a primary carrying no track — the redirect lane, and
    the fixture `test_promote_does_not_claim_commerce_ready_off_the_money_lane`
    needs; a test whose subject is the promotion COUNT passes the money lane so it
    keeps counting promotions."""
    executed: List[Dict[str, Any]] = []

    async def fake_fetch_one(sql, params=None):
        # Group primary lookup returns our standard fixture
        return {
            "product_key": "prod::external_seed::external_seed::ext_pk_x",
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_product_id": "ext_pk_x",
            "parent_title": "Foundation",
            "catalog_track": catalog_track,
            "product_payload": None,
            "seed_data": {"snapshot": {"variants": [
                _tomford_variant("V1"), _tomford_variant("V2"), _moyu_default_variant(),
            ]}},
        }

    async def fake_execute(sql, params=None):
        executed.append({"sql": str(sql), "params": dict(params or {})})
        return None

    class _DB:
        def fetch_one(self, sql, params=None):
            return fake_fetch_one(sql, params)

        def execute(self, sql, params=None):
            return fake_execute(sql, params)

        def transaction(self):
            return _FakeTxn()

    monkeypatch.setattr(promoter, "database", _DB())
    return executed


@pytest.mark.asyncio
async def test_promote_dry_run_does_not_execute(monkeypatch) -> None:
    """Default invocation (apply=False) never writes."""
    executed = _install_fake_db(monkeypatch, catalog_track="internal_merchant")
    out = await promoter.promote_variants_for_group(group_id="pg_x", apply=False)
    assert executed == []
    # Two real variants found (V1, V2); MOYU default filtered out
    assert out.variants_found == 3
    assert out.variants_promoted == 2  # would-promote count, not actually written
    # money lane: the tier can move, so nothing is held. A BOUND — unchanged by the
    # tier-held split — so `getattr`, or it would die on the harness rather than on
    # the behaviour when run against a promoter that predates the counter.
    assert getattr(out, "variants_tier_held", 0) == 0
    assert out.skipped_reason is None


@pytest.mark.asyncio
async def test_the_dry_run_predicts_the_tier_held_split_too(monkeypatch) -> None:
    """A PREVIEW THAT OVERSTATES IS THE SAME LIE AS A REPORT THAT DOES. The same
    fixture on the REDIRECT lane: `promoted_readiness_tier` offers 'referral_only',
    which is the FLOOR of `index_graduation_ladder.OBSERVED_READINESS_LADDER`, so
    `UPSERT_SKU_SQL`'s upward-only CASE cannot raise any stored tier and an INSERT
    mints the floor. Nothing on this lane can be promoted, so a dry run must not
    say two rows would be — it says two rows would be WRITTEN with the tier held."""
    executed = _install_fake_db(monkeypatch)  # no catalog_track — the redirect lane
    out = await promoter.promote_variants_for_group(group_id="pg_x", apply=False)
    assert executed == []
    assert out.variants_found == 3
    assert out.variants_promoted == 0, (
        "the dry run claimed a promotion on a lane where the tier cannot move"
    )
    assert out.variants_tier_held == 2


@pytest.mark.asyncio
async def test_promote_apply_upserts_per_real_variant(monkeypatch) -> None:
    """Apply path: one UPSERT per real variant. NO writes to
    catalog_products / seed_data / product_payload."""
    executed = _install_fake_db(monkeypatch, catalog_track="internal_merchant")
    out = await promoter.promote_variants_for_group(group_id="pg_x", apply=True)
    assert out.variants_promoted == 2
    upserts = [e for e in executed if "INSERT INTO catalog_skus" in e["sql"]]
    assert len(upserts) == 2
    sql_joined = "\n".join(e["sql"] for e in executed)
    assert "catalog_products" not in sql_joined
    assert "external_product_seeds" not in sql_joined
    assert "seed_data" not in sql_joined
    assert "product_payload" not in sql_joined
    # ON CONFLICT clause present so re-runs are idempotent — and it must name the
    # index that EXISTS. Migration 123 replaced the 3-column
    # (merchant_id, platform, source_variant_id) index with the 4-column
    # idx_catalog_skus_source_identity_v2, and Postgres rejects an ON CONFLICT
    # clause matching no unique constraint at parse time (42P10), so the old
    # target made `promote_variants_all` unexecutable. This assertion is a string
    # check on a fake DB and could not have caught that; the executing proof is
    # tests/test_sku_identity_upserts_postgres.py.
    assert (
        "ON CONFLICT (merchant_id, platform, product_key, source_variant_id)"
        in sql_joined
    )
    # And the DO UPDATE must rename nothing: the 4,286 rows the 2026-09-08
    # variant-identity backfill adopted carry live catalog_offers keyed on their
    # sku_key, and catalog_offers has no FK to catch a rename.
    for renamed in ("sku_key = EXCLUDED", "product_key = EXCLUDED",
                    "source_product_id = EXCLUDED"):
        assert renamed not in sql_joined, renamed


@pytest.mark.asyncio
async def test_promote_does_not_claim_commerce_ready_off_the_money_lane(monkeypatch) -> None:
    """`readiness_tier` is BOUND now, not the literal 'commerce_ready' this lane
    used to write on every product it touched.

    The candidate query filters on `product_group_id LIKE 'pg_%'` and nothing else,
    so external-seed products are promoted here too; #2139 measured 5,083
    external-seed SKU rows holding 'commerce_ready' that should hold
    'referral_only', and names this writer. This fixture's primary carries no
    `catalog_track` at all, which `promoted_readiness_tier` treats as the redirect
    lane on purpose: the function is an ALLOWLIST of `internal_merchant`, so an
    unknown, absent or not-yet-invented track gets the floor. Over-claiming a tier
    fabricates a purchasable SKU; under-claiming one only understates a row another
    lane can still promote.

    A behavioural pin on the params, not a string check on the SQL — the executing
    proof of the tier's UPWARD-ONLY move is
    tests/test_sku_identity_upserts_postgres.py, which needs a real Postgres."""
    executed = _install_fake_db(monkeypatch)
    out = await promoter.promote_variants_for_group(group_id="pg_x", apply=True)
    upserts = [e for e in executed if "INSERT INTO catalog_skus" in e["sql"]]
    assert upserts, "nothing was written; this test would be vacuous"
    assert {e["params"]["readiness_tier"] for e in upserts} == {"referral_only"}
    # ...and the rows are not counted as promotions: 'referral_only' is the ladder
    # FLOOR, so the upward-only CASE moved nothing.
    assert out.variants_promoted == 0
    assert out.variants_tier_held == len(upserts)


@pytest.mark.asyncio
async def test_promote_skips_when_no_real_variants(monkeypatch) -> None:
    """A group whose primary has only Default-Title placeholder
    variants → skipped, no upserts. MOYU's case."""
    executed: List[Dict[str, Any]] = []

    async def fake_fetch_one(sql, params=None):
        return {
            "product_key": "p_moyu",
            "merchant_id": "merch_moyu",
            "platform": "shopify",
            "source_product_id": "10064565",
            "parent_title": "Foundation Brush",
            "product_payload": {"variants": [_moyu_default_variant()]},
            "seed_data": None,
        }

    async def fake_execute(sql, params=None):
        executed.append({"sql": str(sql), "params": params})

    class _DB:
        def fetch_one(self, sql, params=None): return fake_fetch_one(sql, params)
        def execute(self, sql, params=None): return fake_execute(sql, params)
        def transaction(self): return _FakeTxn()

    monkeypatch.setattr(promoter, "database", _DB())

    out = await promoter.promote_variants_for_group(group_id="pg_moyu", apply=True)
    assert out.skipped_reason == "no_real_variants"
    assert out.variants_promoted == 0
    assert executed == []


@pytest.mark.asyncio
async def test_promote_skips_when_no_primary(monkeypatch) -> None:
    """A group with no primary row (data integrity bug, race) → skip
    gracefully; don't crash."""
    async def fake_fetch_one(sql, params=None):
        return None

    class _DB:
        def fetch_one(self, sql, params=None): return fake_fetch_one(sql, params)
        def execute(self, sql, params=None): return None
        def transaction(self): return _FakeTxn()

    monkeypatch.setattr(promoter, "database", _DB())

    out = await promoter.promote_variants_for_group(group_id="pg_orphan", apply=True)
    assert out.skipped_reason == "no_primary_for_group"
    assert out.variants_promoted == 0


@pytest.mark.asyncio
async def test_promote_prefers_path_b_seed_when_both_paths_have_data(monkeypatch) -> None:
    """When both seed_data.snapshot.variants and product_payload.variants
    are present, prefer seed_data (Path B has richer axis_kind metadata)."""
    async def fake_fetch_one(sql, params=None):
        return {
            "product_key": "p_x",
            "merchant_id": "m", "platform": "x", "source_product_id": "s",
            "parent_title": "X",
            # the money lane, so `variants_promoted` below still counts promotions
            "catalog_track": "internal_merchant",
            "product_payload": {"variants": [_moyu_default_variant()]},  # 1 default
            "seed_data": {"snapshot": {"variants": [_tomford_variant("real_1"), _tomford_variant("real_2")]}},
        }

    executed: List[Dict[str, Any]] = []

    async def fake_execute(sql, params=None):
        executed.append({"sql": str(sql), "params": dict(params or {})})

    class _DB:
        def fetch_one(self, sql, params=None): return fake_fetch_one(sql, params)
        def execute(self, sql, params=None): return fake_execute(sql, params)
        def transaction(self): return _FakeTxn()

    monkeypatch.setattr(promoter, "database", _DB())

    out = await promoter.promote_variants_for_group(group_id="pg_x", apply=True)
    # Path B's 2 real variants used; Path A's default ignored
    assert out.variants_promoted == 2


# ---------------------------------------------------------------------------
# promote_variants_all — driver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_all_does_not_count_a_tier_held_group_as_promoted(monkeypatch) -> None:
    """THE GROUP COUNT IS SPLIT THE SAME WAY THE SKU COUNT IS. A group on the
    redirect lane is WRITTEN — every row `variants_tier_held`, none promoted — and
    `groups_promoted` used to count it anyway, so a run over an all-external-seed
    corpus said `groups_promoted: N` two lines above `skus_upserted_total: 0`. A
    group is promoted only if a row in it could be."""
    outcomes = {
        "pg_held": promoter.GroupOutcome(
            product_group_id="pg_held", primary_product_key="pk_held", variants_found=2, variants_promoted=0, variants_tier_held=2),
        "pg_money": promoter.GroupOutcome(
            product_group_id="pg_money", primary_product_key="pk_money", variants_found=2, variants_promoted=2, variants_tier_held=0),
        "pg_skip": promoter.GroupOutcome(
            product_group_id="pg_skip", primary_product_key="pk_skip", variants_found=0, variants_promoted=0,
            skipped_reason="no_real_variants"),
    }

    async def fake_fetch_all(sql, params=None):
        return [{"group_id": g} for g in outcomes]

    async def fake_promote(group_id, apply):
        return outcomes[group_id]

    monkeypatch.setattr(promoter.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(promoter, "promote_variants_for_group", fake_promote)

    report = await promoter.promote_variants_all(limit=0)
    assert report.groups_considered == 3
    assert report.groups_promoted == 1, "a tier-held group was counted as promoted"
    assert report.groups_tier_held == 1
    assert report.groups_skipped_no_real_variants == 1
    assert report.skus_upserted_total == 2
    assert report.skus_tier_held_total == 2


@pytest.mark.asyncio
async def test_promote_all_iterates_groups_with_scope(monkeypatch) -> None:
    """promote_variants_all takes merchant_id / product_group_id /
    limit. Pin that the SQL builder respects scope."""
    captured: List[Dict[str, Any]] = []

    async def fake_fetch_all(sql, params=None):
        captured.append({"sql": str(sql), "params": dict(params or {})})
        return []

    monkeypatch.setattr(promoter.database, "fetch_all", fake_fetch_all)

    await promoter.promote_variants_all(merchant_id="external_seed", limit=10)
    assert "pgm.merchant_id = :merchant_id" in captured[0]["sql"]
    assert captured[0]["params"]["merchant_id"] == "external_seed"
    assert captured[0]["params"]["limit"] == 10

    captured.clear()
    await promoter.promote_variants_all(product_group_id="pg_x", limit=0)
    # group_id scope dominates when both provided
    assert "pgm.product_group_id = :group_id" in captured[0]["sql"]
    # limit=0 → no LIMIT clause
    assert "LIMIT :limit" not in captured[0]["sql"]
