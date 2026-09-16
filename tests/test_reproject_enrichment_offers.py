"""The enrichment-cohort offer re-projection.

The mirror reconciler cannot reach these offers: every one of its queries filters
`source_system = 'external_product_seeds_mirror_v1'`, and its writer emits one offer
per seed at `::canonical`. Measured on prod 2026-09-16, 10,525 of 16,764
serving-eligible priced offers (63%) sit outside it, and the mirror cohort holds zero
variant rows.

These tests pin the two things a repair like this gets wrong: which price a given
offer should be aligned to, and which readings it must refuse rather than write.
"""

import json
from decimal import Decimal
from typing import Any, Dict, List

import pytest

import scripts.reproject_enrichment_offers as rep


def _offer(sku_key: str, offer_price: str, seed_price: str = "28.80",
           variants: List[Dict[str, Any]] = None, offer_currency: str = "SGD",
           seed_currency: str = "SGD") -> Dict[str, Any]:
    return {
        "offer_id": "offer:x:" + sku_key[-12:],
        "sku_key": sku_key,
        "offer_price": Decimal(offer_price),
        "offer_currency": offer_currency,
        # NOT coerced: the point of several cases below is a value the DB layer can
        # hand back that Decimal() cannot parse. Coercing here would raise in the
        # fixture and test nothing.
        "seed_price": seed_price,
        "seed_currency": seed_currency,
        "seed_data": json.dumps({"variants": variants or []}),
    }


PK = "ext:jungsaemmool-lip-pression-metal-serum-gloss::66a3c8a4"
VID = "50856826536257"


def test_a_variant_offer_aligns_to_its_OWN_variant_not_the_scalar() -> None:
    """The scalar describes the product; a shade has its own price. Aligning a
    variant offer to the scalar would publish one shade's price on all of them."""
    verdict = rep.classify(_offer(
        f"{PK}::v:{VID}", "28.20", seed_price="99.00",
        variants=[{"variant_id": VID, "price_amount": "28.80", "price_currency": "SGD"}],
    ))
    assert verdict["action"] == "repair"
    assert verdict["scope"] == "variant"
    assert verdict["target"] == Decimal("28.80"), "must take the VARIANT price, not 99.00"


def test_a_canonical_offer_aligns_to_the_scalar() -> None:
    verdict = rep.classify(_offer(f"{PK}::canonical", "28.20", seed_price="28.80"))
    assert verdict["action"] == "repair"
    assert verdict["scope"] == "canonical"
    assert verdict["target"] == Decimal("28.80")


def test_a_variant_the_seed_does_not_describe_is_left_alone() -> None:
    """This tool cannot judge a shade the seed says nothing about."""
    verdict = rep.classify(_offer(
        f"{PK}::v:{VID}", "28.20",
        variants=[{"variant_id": "some-other-id", "price_amount": "10.00"}],
    ))
    assert verdict["action"] == "skip"
    assert verdict["reason"] == "no_seed_variant"


def test_an_offer_that_already_agrees_is_not_rewritten() -> None:
    """CONTROL. Without this, every test above would also pass if the tool returned
    `repair` unconditionally -- rewriting all 16,764 offers to the value they hold."""
    assert rep.classify(_offer(f"{PK}::canonical", "28.80", seed_price="28.80"))["reason"] == "already_agrees"
    assert rep.classify(_offer(
        f"{PK}::v:{VID}", "28.80",
        variants=[{"variant_id": VID, "price_amount": "28.80"}],
    ))["reason"] == "already_agrees"


def test_decimal_formatting_is_not_a_difference() -> None:
    assert rep.classify(_offer(f"{PK}::canonical", "28.80", seed_price="28.8"))["reason"] == "already_agrees"


@pytest.mark.parametrize("bad", ["0", "0.00", "-1"])
def test_a_non_positive_seed_price_is_refused(bad: str) -> None:
    """The extractor returns 0.0 for a price glyph it could not read a number out
    of. Writing that would erase a price the serving gate needs."""
    assert rep.classify(_offer(f"{PK}::canonical", "28.20", seed_price=bad))["reason"] == "non_positive"


def test_a_currency_mismatch_is_refused_not_redenominated() -> None:
    v = rep.classify(_offer(f"{PK}::canonical", "24000", seed_price="24.00",
                            offer_currency="KRW", seed_currency="USD"))
    assert v["reason"] == "currency_mismatch"


def test_a_variant_currency_mismatch_is_refused_too() -> None:
    v = rep.classify(_offer(
        f"{PK}::v:{VID}", "24000", offer_currency="KRW",
        variants=[{"variant_id": VID, "price_amount": "24.00", "price_currency": "USD"}],
    ))
    assert v["reason"] == "currency_mismatch"


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "not-a-price", ""])
def test_an_unreadable_seed_price_is_refused_and_never_raises(bad: str) -> None:
    v = rep.classify(_offer(f"{PK}::canonical", "28.20", seed_price=bad))
    assert v["action"] == "skip"


def test_the_mirror_cohort_is_not_touched() -> None:
    """It has an owner already. Two writers repairing one row is how the two offer
    formats diverged in the first place."""
    assert "external_product_seeds_mirror_v1" not in rep.REPAIRABLE_SOURCE_SYSTEMS
    assert "catalog_enrichment_agent_v1" in rep.REPAIRABLE_SOURCE_SYSTEMS


def test_the_update_is_pinned_to_the_price_it_read() -> None:
    """A repair must not overwrite a value that moved since it was read -- the
    nightly refresh runs against the same rows."""
    assert "AND list_price = :expected_price" in rep.UPDATE_SQL


def test_the_total_is_counted_without_the_row_limit() -> None:
    """The count is the whole value of a dry run. Capping the rows and the count
    together reports the same number forever."""
    # Not "no LIMIT anywhere": the count's correlated LATERAL carries a LIMIT 1, which
    # picks one seed per product and caps nothing. The property is that the PAGE size
    # never reaches the count.
    assert ":page_size" not in rep.CANDIDATE_COUNT_SQL
    assert ":cursor" not in rep.CANDIDATE_COUNT_SQL
    assert "LIMIT :page_size" in rep.CANDIDATE_SQL


def test_the_cohort_is_walked_in_pages_never_fetched_whole() -> None:
    """Every candidate row carries its seed's whole seed_data blob. Fetching the
    cohort in one statement was an out-of-memory crash on a Cloud Run job --
    measured: 20 rows fine, 30,000 killed the container."""
    assert "co.offer_id > :cursor" in rep.CANDIDATE_SQL
    assert "ORDER BY co.offer_id" in rep.CANDIDATE_SQL


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(monkeypatch) -> None:
    executed: List[Any] = []

    async def fake_fetch_one(_q, values=None):
        return {"n": 400}

    async def fake_fetch_all(_q, values=None):
        # One page then exhaustion: the walk is a keyset loop, so a stub that
        # returned the same page forever would never terminate.
        return [] if (values or {}).get("cursor") else [_offer(f"{PK}::canonical", "28.20", seed_price="28.80")]

    async def fake_execute(_q, values=None):
        executed.append(values)

    monkeypatch.setattr(rep.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(rep.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(rep.database, "execute", fake_execute)

    report = await rep.run(apply=False, limit=10, scope="all", sample_limit=5)

    assert report["apply"] is False
    assert report["candidate_offers"] == 400, "the total comes from COUNT, not len(rows)"
    assert report["repairable"] == 1
    assert report["repaired"] == 0
    assert executed == [], "a dry run must not write"


@pytest.mark.asyncio
async def test_an_apply_writes_the_variant_price_pinned_to_the_old_one(monkeypatch) -> None:
    executed: List[Dict[str, Any]] = []

    async def fake_fetch_one(_q, values=None):
        return {"n": 1}

    async def fake_fetch_all(_q, values=None):
        if (values or {}).get("cursor"):
            return []
        return [_offer(
            f"{PK}::v:{VID}", "28.20",
            variants=[{"variant_id": VID, "price_amount": "28.80", "price_currency": "SGD"}],
        )]

    async def fake_execute(_q, values=None):
        executed.append(dict(values or {}))

    monkeypatch.setattr(rep.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(rep.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(rep.database, "execute", fake_execute)

    report = await rep.run(apply=True, limit=10, scope="variant", sample_limit=5)

    assert report["repaired"] == 1
    assert executed[0]["price"] == Decimal("28.80")
    assert executed[0]["expected_price"] == Decimal("28.20")


@pytest.mark.asyncio
async def test_scope_selects_which_half_runs(monkeypatch) -> None:
    """The two halves are correct at different times: the canonical half is
    repairable now, the variant half only after the seed variants are refreshed."""
    async def fake_fetch_one(_q, values=None):
        return {"n": 2}

    async def fake_fetch_all(_q, values=None):
        if (values or {}).get("cursor"):
            return []
        return [
            _offer(f"{PK}::canonical", "28.20", seed_price="28.80"),
            _offer(f"{PK}::v:{VID}", "28.20",
                   variants=[{"variant_id": VID, "price_amount": "28.80"}]),
        ]

    monkeypatch.setattr(rep.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(rep.database, "fetch_all", fake_fetch_all)

    canonical = await rep.run(apply=False, limit=0, scope="canonical", sample_limit=5)
    variant = await rep.run(apply=False, limit=0, scope="variant", sample_limit=5)
    both = await rep.run(apply=False, limit=0, scope="all", sample_limit=5)

    assert canonical["repairable"] == 1
    assert variant["repairable"] == 1
    assert both["repairable"] == 2


# --- one row per offer, and no arbitrary price ------------------------------


def test_a_product_whose_active_seeds_disagree_is_refused() -> None:
    """66 products carry active seeds that disagree about the price. Picking one is
    how a repair writes a number nobody asserted."""
    row = _offer(f"{PK}::canonical", "55.00", seed_price="35.75")
    row["distinct_prices"] = 3
    verdict = rep.classify(row)
    assert verdict["action"] == "skip"
    assert verdict["reason"] == "ambiguous_seed_price"


def test_a_single_agreed_price_is_still_repaired() -> None:
    """CONTROL: the guard must not refuse everything."""
    row = _offer(f"{PK}::canonical", "28.20", seed_price="28.80")
    row["distinct_prices"] = 1
    assert rep.classify(row)["action"] == "repair"


def test_a_missing_distinct_count_is_treated_as_unambiguous() -> None:
    row = _offer(f"{PK}::canonical", "28.20", seed_price="28.80")
    assert rep.classify(row)["action"] == "repair"


def test_the_ambiguity_column_is_actually_SELECTED() -> None:
    """The guard reads `distinct_prices`. The first version computed it inside the
    LATERAL and never added it to the outer SELECT, so classify() read None, defaulted
    to 1, and the guard never fired -- while the unit test above passed, because it
    sets the field by hand. A predicate that reads a column the query does not project
    is not a guard."""
    assert "s.distinct_prices" in rep.CANDIDATE_SQL
    assert "AS distinct_prices" in rep.CANDIDATE_SQL


def test_the_seed_is_collapsed_to_one_row_per_offer() -> None:
    """A plain JOIN produced one candidate PER ATTACHED SEED. That made the target
    arbitrary, attempted the same offer repeatedly, and -- because execute() does not
    raise when the pinned UPDATE matches nothing -- reported 50 repairs for 27 writes.
    """
    assert "JOIN LATERAL" in rep.CANDIDATE_SQL
    assert "COUNT(DISTINCT ROUND(s2.price_amount" in rep.CANDIDATE_SQL
    assert "JOIN external_product_seeds s ON" not in rep.CANDIDATE_SQL
    assert "JOIN external_product_seeds s ON" not in rep.CANDIDATE_COUNT_SQL
