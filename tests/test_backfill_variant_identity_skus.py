"""The backfill writes to the live catalog, so its refusals are the feature.

Every test here corresponds to a defect an adversarial review found in the first draft of
scripts/backfill_variant_identity_skus.py — a draft that had no tests, which is why the defects
reached main. Each one is written so it fails if the fix is reverted.
"""

import collections
import inspect
import json

import pytest

import scripts.backfill_variant_identity_skus as backfill
from scripts.backfill_variant_identity_skus import (
    SELECT_LIVE_OFFERS_SQL,
    SELECT_PRODUCTS_SQL,
    UPSERT_OFFER_SQL,
    UPSERT_SKU_SQL,
    _availability_of,
    _price_of,
    choose_offer,
    normalize_availability,
    plan_for_product,
)
from services.catalog_enrichment_agent.ingestion import derive_offer_id
from services.variant_identity import MERCHANT_ISSUED


def _sql(text):
    return " ".join(text.lower().split())


def _write_path_source():
    """The whole module, not one function.

    These ratchets used to call `inspect.getsource(backfill.run)`. Splitting the scan loop
    into a helper then blinded every one of them at once while they stayed green — a text
    ratchet aimed at a function name is only as stable as the refactor that moves it."""
    return inspect.getsource(backfill)


# ---------------------------------------------------------------------------
# B1 — the conflict target
# ---------------------------------------------------------------------------


def test_sku_upsert_conflicts_on_the_identity_index_not_the_primary_key():
    """catalog_skus has TWO unique constraints and Postgres infers only the one named.
    `ON CONFLICT (sku_key)` collided with 4,971 promoter-written rows carrying the same
    (merchant_id, platform, product_key, source_variant_id) under a different sku_key
    spelling, and every one raised unique_violation instead of upserting."""
    sql = _sql(UPSERT_SKU_SQL)
    assert "on conflict (merchant_id, platform, product_key, source_variant_id)" in sql
    assert "on conflict (sku_key)" not in sql


def test_sku_upsert_returns_the_key_that_actually_holds_the_identity():
    """When the identity already lives under the promoter's spelling we adopt that row —
    so the offer must be attached to the RETURNED sku_key, not the one we computed."""
    assert _sql(UPSERT_SKU_SQL).endswith("returning sku_key")


def test_sku_upsert_merges_the_payload_and_survives_a_null_column():
    """`sku_payload = EXCLUDED.sku_payload` would destroy agent_version / source_handle /
    canonical_url on a row another writer created, and relabel it as this batch's.

    The coalesce is the second half: `NULL || jsonb` is NULL in Postgres, so a row whose
    sku_payload column is SQL NULL would have the provenance marker this backfill exists to
    write silently ERASED. All 27,268 rows are objects today and the column is nullable."""
    sql = _sql(UPSERT_SKU_SQL)
    assert (
        "sku_payload = coalesce(catalog_skus.sku_payload, '{}'::jsonb) || excluded.sku_payload"
        in sql
    )


# ---------------------------------------------------------------------------
# B2 — suppression
# ---------------------------------------------------------------------------


def test_offer_selection_filters_suppressed_rather_than_merely_preferring_live():
    """`ORDER BY (suppressed_at IS NULL) DESC` still returns a suppressed row when every
    row is suppressed — 573 products on prod. Cloning it created unsuppressed priced supply
    under a seller a human had withdrawn."""
    sql = _sql(SELECT_LIVE_OFFERS_SQL)
    assert "suppressed_at is null" in sql
    assert "where" in sql.split("suppressed_at is null")[0]
    assert "order by (suppressed_at is null)" not in sql


def test_product_scan_skips_suppressed_products():
    assert "cp.suppressed_at is null" in _sql(SELECT_PRODUCTS_SQL)


def test_a_product_with_no_live_offer_is_refused_not_guessed():
    chosen, reason = choose_offer([])
    assert chosen is None and reason == "no_live_offer"


# ---------------------------------------------------------------------------
# B3 — the seed join
# ---------------------------------------------------------------------------


def test_the_seed_join_is_scoped_to_this_product_and_to_active_seeds():
    """Unscoped, it matched 652 seeds whose attached_product_key names a DIFFERENT product
    and 1,660 inactive ones. A real merchant variant id borrowed from the wrong product still
    classifies MERCHANT_ISSUED — variant_identity cannot catch this, because it arrives
    through the join rather than the string."""
    sql = _sql(SELECT_PRODUCTS_SQL)
    assert "eps.attached_product_key = cp.product_key" in sql
    assert "eps.status = 'active'" in sql


# ---------------------------------------------------------------------------
# B4 — offer id stability
# ---------------------------------------------------------------------------


def test_offer_id_is_derived_from_the_destination_not_the_chosen_offer_row():
    """B4. A first version of this test called derive_offer_id twice with the same arguments
    and asserted they matched — which tests that a hash is a function, not that the CALL SITE
    stopped hashing the chosen offer's id. A mutation reverting B4 survived it. This reads the
    source of the call instead, which is the only check available without a Postgres."""
    src = _write_path_source()
    call = src.split("derive_offer_id(", 1)[1].split(")", 1)[0]
    assert "destination" in call, f"offer_id no longer derives from the destination: {call!r}"
    assert "offer_id" not in call, (
        "offer_id is being derived from the chosen offer row again — 631 products tie on the "
        f"ordering with no tiebreaker, so re-runs write duplicates: {call!r}"
    )


def test_offer_upsert_restamps_source_system_on_the_update_path():
    """Without this a re-run rewrites prices and leaves the previous writer's stamp, so the
    change is untraceable. (The first version of this assertion was self-cancelling: it or-ed
    a string against a .replace() of itself, so it was true whenever it was true.)"""
    assert "source_system" in _sql(UPSERT_OFFER_SQL).split("do update set", 1)[1]


def test_apply_refuses_to_run_without_the_contract_token():
    """A stale image runs the MERGED first draft, which has all four blockers and no such flag.
    argparse then refuses the command instead of silently running the broken version."""
    assert backfill.CONTRACT == "backfill-v2-identity-index"
    src = _write_path_source()
    assert "--expect-contract" in src
    assert "args.apply and args.expect_contract != CONTRACT" in src


# ---------------------------------------------------------------------------
# H2 — the availability vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("in_stock", "in_stock"),
        ("In Stock", "in_stock"),        # 636 prod rows
        ("out_of_stock", "out_of_stock"),
        ("Out of Stock", "out_of_stock"),  # 93 prod rows — the dangerous one
        ("sold out", "out_of_stock"),
        ("low stock", "low_stock"),
        ("", "unknown"),
        ("something we have never seen", "unknown"),
    ],
)
def test_availability_lands_in_the_closed_vocabulary(raw, expected):
    assert normalize_availability(raw) == expected


def test_a_sold_out_variant_reads_as_sold_out():
    """The specific failure: raw text passed through, so "Out of Stock" was not
    out_of_stock and a sold-out variant looked buyable."""
    assert _availability_of({"availability": "Out of Stock"}, "in_stock") == "out_of_stock"
    assert _availability_of({"in_stock": False}, None) == "out_of_stock"


def test_availability_never_returns_something_outside_the_vocabulary():
    allowed = {"in_stock", "out_of_stock", "low_stock", "unknown"}
    for raw in ["In Stock", "weird", "", None, "LOW STOCK", "sold_out"]:
        assert normalize_availability(raw) in allowed


# ---------------------------------------------------------------------------
# H4 — seller attribution
# ---------------------------------------------------------------------------


def test_a_multi_merchant_product_is_refused_not_attributed_to_one_seller():
    """729 prod products have live offers from more than one merchant. Nothing about a
    variant makes it belong to whichever seller sorted first."""
    chosen, reason = choose_offer([
        {"merchant_id": "m_a", "destination_url": "https://a.com/p"},
        {"merchant_id": "m_b", "destination_url": "https://b.com/p"},
    ])
    assert chosen is None and reason == "multi_merchant_product"


def test_a_single_merchant_product_is_accepted():
    chosen, reason = choose_offer([
        {"merchant_id": "m_a", "destination_url": "https://a.com/p"},
        {"merchant_id": "m_a", "destination_url": "https://a.com/p2"},
    ])
    assert reason is None and chosen["merchant_id"] == "m_a"


def test_an_offer_with_no_destination_is_refused():
    """H1: the destination was never carried, so every new offer was a dead end that
    agent_shop_gateway filters out. Refusing is better than writing one."""
    chosen, reason = choose_offer([{"merchant_id": "m_a", "destination_url": ""}])
    assert chosen is None and reason == "no_destination_url"


# ---------------------------------------------------------------------------
# Price and provenance refusals
# ---------------------------------------------------------------------------


def test_price_comes_from_the_variant_and_zero_is_not_a_price():
    assert _price_of({"price_amount": "24.00"}) == 24.0
    assert _price_of({"price": "1,299.00"}) == 1299.0
    assert _price_of({"price": 0}) is None
    assert _price_of({"price": "not a number"}) is None
    assert _price_of({}) is None


def _row(variants):
    return {
        "product_key": "ext:brand-thing::abc12345",
        "source_product_id": "brand-thing",
        "seed_variants": json.dumps(variants),
        "payload_variants": None,
        "title": "Thing",
    }


def test_plan_keeps_merchant_issued_priced_variants_only():
    """Each rejected variant carries a REAL shade title, so filter_real_variants admits it and
    the guard under test is the only thing that can refuse it. A first draft of this test gave
    the fabricated-id variant the title "Default", which the placeholder filter removed
    upstream — the provenance counter stayed 0 and the test proved nothing about provenance."""
    counts = collections.Counter()
    picks = plan_for_product(
        _row([
            {"variant_id": "43062643884185", "title": "Peach", "price": "24.00"},
            {"variant_id": "brand-thing-default", "title": "Rose", "price": "24.00"},
            {"variant_id": "43062643884186", "title": "Berry"},
        ]),
        counts,
    )
    assert [p["variant_id"] for p in picks] == ["43062643884185"]
    assert counts["skipped_not_merchant_issued"] == 1
    assert counts["skipped_no_variant_price"] == 1


def test_plan_records_every_refusal_rather_than_dropping_silently():
    counts = collections.Counter()
    plan_for_product(_row([{"variant_id": "brand-thing", "title": "x", "price": "1"}]), counts)
    # `sum(counts.values()) >= 1` passed if ANY counter moved. Name the one that must.
    assert counts["skipped_not_merchant_issued"] == 1


def test_the_offer_is_attached_to_the_returned_key_not_the_computed_one():
    """B1's consumption half. `RETURNING sku_key` is pointless if the offer is then attached to
    the key we computed: on the 4,287 rows that adopt the promoter's `::v::` spelling the offer
    would hang off a sku_key that does not exist. A mutation doing exactly that survived the
    original suite, because nothing read run()."""
    src = _write_path_source()
    body = src.split("offer_params = {", 1)[1]
    sku_key_line = [l for l in body.splitlines() if '"sku_key"' in l][0]
    assert "written_key" in sku_key_line, (
        f"offer is not attached to the RETURNING key: {sku_key_line.strip()!r}"
    )


def test_a_guard_rejection_rolls_the_sku_back_instead_of_orphaning_it():
    """The SKU is INSERTed before the guard runs. Committing when the guard rejects the offer
    leaves precisely the orphan SKU-without-offer state this backfill exists to remove."""
    src = _write_path_source()
    assert "raise _OfferRefused()" in src
    assert "rolled_back_offer_refused_by_guard" in src


# ---------------------------------------------------------------------------
# M5 — reproducibility
# ---------------------------------------------------------------------------


def test_the_scan_is_ordered_so_limit_and_resume_are_reproducible():
    """Without ORDER BY, --limit selected a nondeterministic set in heap order, so a phased
    rollout could not be repeated, resumed or audited."""
    sql = _sql(SELECT_PRODUCTS_SQL)
    assert "order by cp.product_key" in sql
    assert "cp.product_key > :after" in sql
