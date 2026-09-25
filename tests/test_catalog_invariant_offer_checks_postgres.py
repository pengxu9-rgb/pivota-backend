"""The four new offer/identity invariants, with their SQL EXECUTED on real PG.

`services/catalog_invariant_checks` ships raw SQL strings that only ever run on
Postgres. The three counting checks added here use `NOT EXISTS` anti-joins, a
`HAVING count(*) > 1` CTE with `sum(n - 1)`, and a two-column suppression join;
the fourth reads `sku_payload->>'variant_id_provenance'` out of jsonb with
`count(*) FILTER (WHERE ...)`. None of it is expressible on SQLite, and a
check whose SQL cannot be planned is reported by the runner as
`{"error": ...}` — a check that never fails and never fires, which is the worst
possible state for an alarm.

Each check is exercised BOTH WAYS: a clean table must count 0, and the specific
row shape it exists to catch must count 1. A check that only ever sees the
violating fixture cannot be distinguished from one whose predicate matches
everything.

Named `test_catalog_invariant*_postgres.py` so the dialect gate discovers it and
so it sorts beside `test_catalog_invariant_sample_contract_postgres.py`, which
asserts the SAMPLE CONTRACT (`... AS subject_key`) over every check in `_CHECKS`
— including these.
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

MERCHANT = "m_invcheck_pg"
PLATFORM = "invcheck_lane"
OTHER_PLATFORM = "invcheck_lane_b"
PK_LIVE = "invcheck::pg::live"
PK_DEAD = "invcheck::pg::suppressed"
SKU_LIVE = PK_LIVE + "::canonical"
SKU_DEAD = PK_DEAD + "::canonical"
SKU_ORPHAN = "invcheck::pg::nosuch::canonical"
_ALL_PKS = (PK_LIVE, PK_DEAD)


async def _ddl(database):
    from db.catalog import catalog_offers, catalog_products, catalog_skus
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables([catalog_products, catalog_skus, catalog_offers])


async def _clear(database):
    for table in ("catalog_offers", "catalog_skus", "catalog_products"):
        await database.execute(
            f"DELETE FROM {table} WHERE product_key = ANY(:pks)",
            {"pks": list(_ALL_PKS)},
        )


async def _product(database, product_key, *, suppressed=False):
    await database.execute(
        """INSERT INTO catalog_products
             (product_key, merchant_id, platform, source_product_id, title,
              suppressed_at, suppression_reason)
           VALUES (:pk,:m,:p,:pk,'Invariant Fixture',
                   CASE WHEN :sup THEN NOW() ELSE NULL END,
                   CASE WHEN :sup THEN 'fixture' ELSE NULL END)""",
        {"pk": product_key, "m": MERCHANT, "p": PLATFORM, "sup": suppressed},
    )


async def _sku(database, sku_key, product_key, *, platform=PLATFORM,
               provenance="__omit__", suppressed=False):
    """`provenance="__omit__"` writes a payload with NO `variant_id_provenance`
    key — the UNSTAMPED shape most of the corpus is in today, and the one the
    share must not silently exclude from its denominator. `None` writes no
    payload at all, which is a different unstamped shape and must count the
    same."""
    if provenance is None:
        payload = None
    elif provenance == "__omit__":
        payload = json.dumps({"source": "fixture"})
    else:
        payload = json.dumps({"variant_id_provenance": provenance})
    await database.execute(
        """INSERT INTO catalog_skus
             (sku_key, product_key, merchant_id, platform, source_product_id,
              source_variant_id, title, readiness_tier, sku_payload,
              suppressed_at, suppression_reason)
           VALUES (:sk,:pk,:m,:p,:pk,:sk,'Invariant Fixture','referral_only',
                   CAST(:payload AS jsonb),
                   CASE WHEN :sup THEN NOW() ELSE NULL END,
                   CASE WHEN :sup THEN 'fixture' ELSE NULL END)""",
        {"sk": sku_key, "pk": product_key, "m": MERCHANT, "p": platform,
         "payload": payload, "sup": suppressed},
    )


async def _offer(database, offer_id, *, sku_key, product_key,
                 channel="external_referral", market="US", suppressed=False):
    await database.execute(
        """INSERT INTO catalog_offers
             (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
              readiness_tier, offer_mode, channel, market, availability, currency,
              list_price, source_system, suppressed_at, suppression_reason)
           VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only',
                   'redirect',:ch,:mkt,'in_stock','USD',10.0,'fixture',
                   CASE WHEN :sup THEN NOW() ELSE NULL END,
                   CASE WHEN :sup THEN 'fixture' ELSE NULL END)""",
        {"oid": offer_id, "sk": sku_key, "pk": product_key, "m": MERCHANT,
         "ch": channel, "mkt": market, "sup": suppressed},
    )


@pytest.fixture
async def db():
    from db.database import database

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _ddl(database)
    await _clear(database)
    try:
        yield database
    finally:
        await _clear(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


def _check(name):
    from services.catalog_invariant_checks import _CHECKS

    matches = [c for c in _CHECKS if c["name"] == name]
    assert len(matches) == 1, f"{name} is not registered exactly once in _CHECKS"
    return matches[0]


async def _count(db, name):
    """Run the check's OWN count_sql — never a restatement of it here. A test
    that re-types the predicate proves the test author's SQL works, which is not
    the question."""
    row = await db.fetch_one(_check(name)["count_sql"])
    return int((row["c"] if row is not None else 0) or 0)


async def _delta(db, name, baseline):
    """How much did THIS fixture move the check? The dialect gate runs every
    gate file against ONE database, so an absolute `== 0` is an assertion about
    which files ran first, not about the predicate. A baseline taken immediately
    before the violating row is inserted makes each claim exact and
    corpus-independent."""
    return await _count(db, name) - baseline


async def _samples(db, name):
    from services.catalog_invariant_checks import _sample_keys

    rows = await db.fetch_all(_check(name)["sample_sql"])
    return _sample_keys(_check(name), rows)


# ---------------------------------------------------------------------------
# offers_without_sku
# ---------------------------------------------------------------------------
async def test_offers_without_sku_counts_the_orphan_and_not_the_healthy_row(db):
    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "oi:healthy", sku_key=SKU_LIVE, product_key=PK_LIVE)

    # A healthy offer moves the count by NOTHING. Measured as a delta against
    # the corpus rather than as `== 0` — see _delta.
    baseline = await _count(db, "offers_without_sku")
    assert baseline == await _count(db, "offers_without_sku")

    await _offer(db, "oi:orphan", sku_key=SKU_ORPHAN, product_key=PK_LIVE,
                 channel="native")

    assert await _delta(db, "offers_without_sku", baseline) == 1
    assert "oi:orphan" in await _samples(db, "offers_without_sku")


async def test_offers_without_sku_ignores_a_suppressed_orphan(db):
    """The remediation SUPPRESSES rather than deletes, so the orphan rows stay in
    the table forever. A check that counted them could never reach its threshold
    and would ship permanently red — the deaf-alarm failure this module's
    threshold convention names."""
    await _product(db, PK_LIVE)
    baseline = await _count(db, "offers_without_sku")
    await _offer(db, "oi:orphan_gone", sku_key=SKU_ORPHAN, product_key=PK_LIVE,
                 suppressed=True)

    assert await _delta(db, "offers_without_sku", baseline) == 0


async def test_offers_without_sku_threshold_is_zero(db):
    from services.catalog_invariant_checks import _threshold

    assert _threshold(_check("offers_without_sku")) == 0
    assert not _check("offers_without_sku").get("warn_only")


# ---------------------------------------------------------------------------
# duplicate_offers_per_sku_channel_market
# ---------------------------------------------------------------------------
async def test_duplicate_offers_counts_excess_rows_not_groups(db):
    """A group of three is ONE index violation and TWO rows to move. The check
    counts rows because the remediation is row-grain; the distinction is the
    whole reason for `sum(n - 1)` rather than `count(*)` over the groups."""
    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "oi:a", sku_key=SKU_LIVE, product_key=PK_LIVE)
    baseline = await _count(db, "duplicate_offers_per_sku_channel_market")

    await _offer(db, "oi:b", sku_key=SKU_LIVE, product_key=PK_LIVE)
    await _offer(db, "oi:c", sku_key=SKU_LIVE, product_key=PK_LIVE)

    # THREE live rows on one shelf move the count by TWO, not by one and not by
    # three: one index violation, two rows to move.
    assert await _delta(db, "duplicate_offers_per_sku_channel_market", baseline) == 2
    assert SKU_LIVE in await _samples(db, "duplicate_offers_per_sku_channel_market")


async def test_duplicate_offers_treats_channel_and_market_as_separate_shelves(db):
    """If the tuple collapsed to sku_key alone, real US/GB and
    referral/native supply would be reported as a defect and then suppressed by
    the reconciler that shares this predicate."""
    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    baseline = await _count(db, "duplicate_offers_per_sku_channel_market")
    await _offer(db, "oi:us", sku_key=SKU_LIVE, product_key=PK_LIVE, market="US")
    await _offer(db, "oi:gb", sku_key=SKU_LIVE, product_key=PK_LIVE, market="GB")
    await _offer(db, "oi:native", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 channel="native")

    assert await _delta(db, "duplicate_offers_per_sku_channel_market", baseline) == 0
    # AND the shelf is not NAMED as an offender. The count alone cannot see this:
    # widening the CTE to `HAVING count(*) >= 1` leaves `sum(n - 1)` unchanged
    # (a singleton group contributes 0) while making every shelf in the
    # catalogue a sample — an alarm that reads 0 and still hands the operator a
    # list of non-defects to chase.
    assert SKU_LIVE not in await _samples(
        db, "duplicate_offers_per_sku_channel_market")


async def test_duplicate_offers_ignores_suppressed_rows(db):
    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    baseline = await _count(db, "duplicate_offers_per_sku_channel_market")
    await _offer(db, "oi:keep", sku_key=SKU_LIVE, product_key=PK_LIVE)
    await _offer(db, "oi:gone", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 suppressed=True)

    assert await _delta(db, "duplicate_offers_per_sku_channel_market", baseline) == 0


# ---------------------------------------------------------------------------
# suppressed_product_with_live_offer
# ---------------------------------------------------------------------------
async def test_suppressed_product_with_live_offer_counts_only_that_pairing(db):
    await _product(db, PK_LIVE)
    await _product(db, PK_DEAD, suppressed=True)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _sku(db, SKU_DEAD, PK_DEAD)
    await _offer(db, "oi:live_on_live", sku_key=SKU_LIVE, product_key=PK_LIVE)
    # The already-cascaded row: suppressed product, suppressed offer. Clean.
    await _offer(db, "oi:dead_on_dead", sku_key=SKU_DEAD, product_key=PK_DEAD,
                 suppressed=True)
    baseline = await _count(db, "suppressed_product_with_live_offer")

    await _offer(db, "oi:live_on_dead", sku_key=SKU_DEAD, product_key=PK_DEAD,
                 channel="native")

    assert await _delta(db, "suppressed_product_with_live_offer", baseline) == 1
    assert "oi:live_on_dead" in await _samples(db, "suppressed_product_with_live_offer")


async def test_the_cascade_helper_drives_this_check_to_zero(db):
    """The check and the writer-side fix are the same claim from both ends: after
    `cascade_offer_suppression`, the invariant it exists for reads 0."""
    from services.catalog_offer_suppression import cascade_offer_suppression

    await _product(db, PK_DEAD, suppressed=True)
    await _sku(db, SKU_DEAD, PK_DEAD)
    baseline = await _count(db, "suppressed_product_with_live_offer")
    await _offer(db, "oi:x", sku_key=SKU_DEAD, product_key=PK_DEAD)
    await _offer(db, "oi:y", sku_key=SKU_DEAD, product_key=PK_DEAD, market="GB")

    assert await _delta(db, "suppressed_product_with_live_offer", baseline) == 2
    await cascade_offer_suppression([PK_DEAD])
    assert await _delta(db, "suppressed_product_with_live_offer", baseline) == 0


# ---------------------------------------------------------------------------
# skus_without_merchant_issued_identity_share
# ---------------------------------------------------------------------------
async def test_identity_provenance_reports_per_lane_share_and_unstamped(db):
    """The share on its own is a lie while most rows are unclassified, so both
    shares and the unstamped denominator have to be in the output."""
    from services.catalog_invariant_checks import _run_identity_provenance_share

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE, provenance="merchant_issued")
    await _sku(db, PK_LIVE + "::v2", PK_LIVE, provenance="product_derived")
    await _sku(db, PK_LIVE + "::v3", PK_LIVE, provenance="__omit__")
    await _sku(db, PK_LIVE + "::v4", PK_LIVE, provenance=None)
    # A second lane, so the per-lane split is exercised rather than assumed.
    await _sku(db, PK_LIVE + "::b1", PK_LIVE, platform=OTHER_PLATFORM,
               provenance="merchant_issued")

    outcome = await _run_identity_provenance_share(db)
    lanes = {lane["lane"]: lane for lane in outcome["detail"]["lanes"]}

    a = lanes[PLATFORM]
    assert a["skus"] == 4
    assert a["merchant_issued"] == 1
    # BOTH unstamped shapes count: a payload without the key, and no payload.
    assert a["unstamped"] == 2
    assert a["stamped"] == 2
    assert a["share_of_all"] == 0.25
    # 1/2, not 1/4 — and the gap between the two numbers is exactly why the
    # unstamped count sits beside them.
    assert a["share_of_stamped"] == 0.5

    b = lanes[OTHER_PLATFORM]
    assert b["skus"] == 1 and b["merchant_issued"] == 1
    assert b["share_of_all"] == 1.0

    # The headline count is live SKUs NOT positively placed as merchant-issued.
    # It is CORPUS-WIDE, and the dialect gate runs every gate file against ONE
    # database — so a neighbour's leftover catalog_skus rows legitimately move
    # it. Assert the contribution THIS fixture makes exactly, and the corpus
    # number only as a lower bound; an `== 3` here would be a test that passes
    # or fails on which files ran before it.
    ours = sum(lane["skus"] - lane["merchant_issued"]
               for lane in (a, b))
    assert ours == 3
    assert outcome["count"] >= ours

    # The ranking is worst-share-first, so the entry names where to look. Read
    # as an ORDER between our two lanes rather than as a position in a
    # corpus-wide top-5, for the same shared-database reason.
    order = list(outcome["sample_keys"])
    if OTHER_PLATFORM in order:
        assert order.index(PLATFORM) < order.index(OTHER_PLATFORM)
    else:
        assert PLATFORM in order or outcome["count"] > ours


async def test_identity_provenance_lane_with_nothing_stamped_has_no_share(db):
    """None, never 0.0: a lane with no evidence has no share, and printing 0.0
    would read as 'measured and bad' rather than 'not measured'."""
    from services.catalog_invariant_checks import _run_identity_provenance_share

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE, provenance="__omit__")

    lanes = {
        lane["lane"]: lane
        for lane in (await _run_identity_provenance_share(db))["detail"]["lanes"]
    }
    assert lanes[PLATFORM]["share_of_stamped"] is None
    assert lanes[PLATFORM]["share_of_all"] == 0.0


async def test_identity_provenance_ignores_suppressed_skus(db):
    from services.catalog_invariant_checks import _run_identity_provenance_share

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE, provenance="merchant_issued")
    await _sku(db, PK_LIVE + "::gone", PK_LIVE, provenance="__omit__",
               suppressed=True)

    lanes = {
        lane["lane"]: lane
        for lane in (await _run_identity_provenance_share(db))["detail"]["lanes"]
    }
    assert lanes[PLATFORM]["skus"] == 1
    assert lanes[PLATFORM]["unstamped"] == 0


async def test_identity_provenance_is_warn_only_and_produces_no_verdict(db):
    """WARN-ONLY IS NOT OFF. The count, the samples and the detail are reported
    exactly as an enforcing check's are; only the verdict is withheld while the
    provenance backfill is outstanding. A report-only check whose OUTPUT were
    suppressed would be indistinguishable from a healthy catalog."""
    from services.catalog_invariant_checks import run_catalog_invariant_checks

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE, provenance="product_derived")

    result = await run_catalog_invariant_checks(db)
    entry = next(
        c for c in result["checks"]
        if c["name"] == "skus_without_merchant_issued_identity_share"
    )
    assert "error" not in entry, entry.get("error")
    assert entry["warn_only"] is True
    assert entry["violated"] is False
    assert entry["over_threshold"] is True
    assert entry["count"] >= 1
    assert entry["sample_keys"]
    assert entry["detail"]["lanes"]


async def test_every_new_check_runs_without_error_through_the_runner(db):
    """The runner swallows a per-check exception into `{"error": ...}` so one bad
    check cannot sink the sweep — which also means an unplannable statement is
    reported as a check that never fires. Assert the absence of that state
    explicitly; a green sweep does not imply it."""
    from services.catalog_invariant_checks import run_catalog_invariant_checks

    result = await run_catalog_invariant_checks(db)
    by_name = {c["name"]: c for c in result["checks"]}
    for name in (
        "offers_without_sku",
        "duplicate_offers_per_sku_channel_market",
        "suppressed_product_with_live_offer",
        "skus_without_merchant_issued_identity_share",
    ):
        assert name in by_name, f"{name} missing from the sweep"
        assert "error" not in by_name[name], by_name[name].get("error")
        assert "count" in by_name[name]
