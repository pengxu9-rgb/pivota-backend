"""The vocabulary repair's write path, EXECUTED — not asserted about as strings.

WHY POSTGRES, AND WHY DRIVEN. Every claim this script makes is about rows it moved, and the
only mechanism that can report that is `UPDATE ... RETURNING` — `databases` + asyncpg returns
NO rowcount from `execute()`, while SQLite does, so a SQLite test of the same code would pass
against a version that reads a rowcount and returns 0 forever in production. `count(*) FILTER`
and the two-table JOIN in the membership census are Postgres-shaped too.

The file is named `test_*_postgres.py` so the dialect gate's glob collects it with no
ride-along edit (`.github/workflows/postgres-dialect-gate.yml`).

FIXTURE DISCIPLINE. The gate runs every `tests/test_*_postgres.py` against ONE database, so
this file builds its tables from the real `db/` models via `ensure_model_tables` and NEVER
drops one: an earlier generation of these fixtures dropped shared tables and killed the next
file in collection order. Teardown deletes this file's ROWS, keyed on its own prefixes.
"""

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: One prefix for everything this file writes, so teardown can be exact and can never reach a
#: neighbour's rows.
P = "vocabfix"
SEED_PRODUCT = P + ":prod:seed"
SHOPIFY_PRODUCT = P + ":prod:shopify"
MERCHANT = P + "_m"

_TABLES = ("catalog_offers", "catalog_skus", "catalog_products", "writer_audit_log")


async def _ddl(database):
    from db.catalog import catalog_offers, catalog_products, catalog_skus, writer_audit_log
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables(
        [catalog_products, catalog_skus, catalog_offers, writer_audit_log]
    )


async def _clear(database):
    """This file's ROWS only. Never its tables — the gate shares one database."""
    await database.execute(
        "DELETE FROM catalog_offers WHERE offer_id LIKE :p", {"p": P + "%"})
    await database.execute(
        "DELETE FROM catalog_skus WHERE sku_key LIKE :p", {"p": P + "%"})
    await database.execute(
        "DELETE FROM catalog_products WHERE product_key LIKE :p", {"p": P + "%"})
    await database.execute(
        "DELETE FROM writer_audit_log WHERE writer_name = :w",
        {"w": "fix_external_track_vocabulary"})


async def _product(database, product_key, platform):
    await database.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform,
             source_product_id, title)
           VALUES (:pk, :m, :pl, :spid, 'Vocab Fixture')""",
        {"pk": product_key, "m": MERCHANT, "pl": platform, "spid": product_key},
    )


async def _sku(database, sku_key, product_key, platform, tier):
    await database.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, readiness_tier)
           VALUES (:sk, :pk, :m, :pl, :spid, :vid, 'Vocab SKU', :tier)""",
        {"sk": sku_key, "pk": product_key, "m": MERCHANT, "pl": platform,
         "spid": product_key, "vid": sku_key, "tier": tier},
    )


async def _offer(database, offer_id, *, track, tier, availability="in_stock",
                 product_key=SEED_PRODUCT):
    await database.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
             catalog_track, truth_tier, readiness_tier, offer_mode, channel, market,
             availability, currency, list_price, source_system, created_at, updated_at)
           VALUES (:oid, :sk, :pk, :m, :track, 'primary', :tier, 'external_referral',
             'default', 'US', :avail, 'USD', 10.0, 'vocab_fixture', NOW(), NOW())""",
        {"oid": offer_id, "sk": offer_id + "::sku", "pk": product_key, "m": MERCHANT,
         "track": track, "tier": tier, "avail": availability},
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


async def _run(db, **kw):
    import scripts.fix_external_track_vocabulary as fix
    return await fix.run(db=db, **{"apply": True, "page": 100, "limit": 0, **kw})


async def _tier(db, table, key_col, key):
    if table == "catalog_offers":
        row = await db.fetch_one(
            "SELECT readiness_tier FROM catalog_offers WHERE offer_id = :k", {"k": key})
    else:
        row = await db.fetch_one(
            "SELECT readiness_tier FROM catalog_skus WHERE sku_key = :k", {"k": key})
    return dict(row)["readiness_tier"]


# --- the vocabulary the SQL literals encode ------------------------------------------------


def test_the_unknown_spelling_is_the_columns_own_default_not_a_guess():
    """`normalize_availability` returns a Python None and never names its stored form.

    This script spells it `unknown`. If that ever stops being the column's server_default, the
    repair would write a value the schema does not consider normal, and no other test here
    could tell — every assertion below would still pass against a consistent wrong string.
    """
    from db.catalog import catalog_offers
    import scripts.fix_external_track_vocabulary as fix

    default = catalog_offers.c.availability.server_default.arg
    assert fix.UNKNOWN == str(default)


def test_the_sql_vocabulary_list_matches_the_python_one():
    """The legal set appears twice — as `LEGAL_AVAILABILITY` and as a literal IN-list inside
    `SELECT_OFFENDING_AVAILABILITY_SQL`. Two spellings of one set drift; this pins them
    together, because a member dropped from the SQL side would make the script silently stop
    seeing (or start rewriting) a whole class of rows."""
    import re

    import scripts.fix_external_track_vocabulary as fix

    in_list = re.search(r"NOT IN \(([^)]*)\)", fix.SELECT_OFFENDING_AVAILABILITY_SQL).group(1)
    assert sorted(re.findall(r"'([^']+)'", in_list)) == sorted(fix.LEGAL_AVAILABILITY)


def test_low_stock_resolves_through_the_vocabulary_to_unknown_not_in_stock():
    """The one judgement call in this PR, pinned at the source.

    `low stock` is the neighbour of `limited stock`, which the vocabulary DOES classify
    in_stock — so mapping it to `in_stock` is the plausible wrong answer, not an absurd one.
    The vocabulary has no `lowstock` member and never infers a positive from an unlisted
    string, so its verdict is None => `unknown`. Asserting both halves, because asserting only
    `== 'unknown'` would also pass if the vocabulary had been edited to classify it that way
    for the wrong reason.
    """
    from utils.availability_vocabulary import normalize_availability
    import scripts.fix_external_track_vocabulary as fix

    assert normalize_availability("low_stock") is None
    assert fix.availability_repair_for("low_stock") == "unknown"
    assert fix.availability_repair_for("low_stock") != "in_stock"


# --- repair 1: external-referral offers -----------------------------------------------------


async def test_it_demotes_a_commerce_ready_external_referral_offer(db):
    await _product(db, SEED_PRODUCT, "external_seed")
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready")

    report = await _run(db)

    assert report["offer_readiness_tier"] == {"planned": 1, "updated": 1}
    assert await _tier(db, "catalog_offers", "offer_id", P + ":o:ext") == "referral_only"


async def test_it_does_not_touch_a_commerce_ready_INTERNAL_merchant_offer(db):
    """The blast-radius bound. A real merchant checkout offer is legitimately commerce_ready,
    and repair 1 is scoped by `catalog_track` alone — if that predicate were ever loosened to
    'every commerce_ready offer', this lane would be silently demoted out of checkout."""
    await _product(db, SEED_PRODUCT, "external_seed")
    await _product(db, SHOPIFY_PRODUCT, "shopify")
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready")
    await _offer(db, P + ":o:int", track="internal_merchant", tier="commerce_ready",
                 product_key=SHOPIFY_PRODUCT)

    report = await _run(db)

    assert report["offer_readiness_tier"] == {"planned": 1, "updated": 1}
    assert await _tier(db, "catalog_offers", "offer_id", P + ":o:int") == "commerce_ready"


# --- repair 2: external-seed SKUs -----------------------------------------------------------


async def test_it_demotes_a_commerce_ready_sku_on_an_external_seed_product(db):
    await _product(db, SEED_PRODUCT, "external_seed")
    await _sku(db, P + ":s:ext", SEED_PRODUCT, "external_seed", "commerce_ready")

    report = await _run(db)

    assert report["sku_readiness_tier"] == {"planned": 1, "updated": 1}
    assert await _tier(db, "catalog_skus", "sku_key", P + ":s:ext") == "referral_only"


async def test_membership_is_the_product_join_not_the_skus_own_platform_column(db):
    """The join choice, made visible in both directions.

    `catalog_skus.platform` is a denormalised copy. A SKU whose copy says external_seed but
    whose PRODUCT is a real Shopify store is NOT in this lane and must survive; a SKU whose
    copy has drifted to something else but whose product IS external_seed must be repaired.
    A predicate written against the SKU column would get both of these backwards.
    """
    await _product(db, SEED_PRODUCT, "external_seed")
    await _product(db, SHOPIFY_PRODUCT, "shopify")
    # copy says external_seed, product says shopify -> out of the lane
    await _sku(db, P + ":s:liar", SHOPIFY_PRODUCT, "external_seed", "commerce_ready")
    # copy drifted, product says external_seed -> in the lane
    await _sku(db, P + ":s:drift", SEED_PRODUCT, "shopify", "commerce_ready")

    report = await _run(db)

    assert report["sku_readiness_tier"] == {"planned": 1, "updated": 1}
    assert await _tier(db, "catalog_skus", "sku_key", P + ":s:drift") == "referral_only"
    assert await _tier(db, "catalog_skus", "sku_key", P + ":s:liar") == "commerce_ready"


# --- repair 3: availability ------------------------------------------------------------------


async def test_it_rewrites_low_stock_to_unknown_and_leaves_the_legal_values_alone(db):
    await _product(db, SEED_PRODUCT, "external_seed")
    await _offer(db, P + ":o:low", track="external_referral", tier="referral_only",
                 availability="low_stock")
    await _offer(db, P + ":o:in", track="external_referral", tier="referral_only",
                 availability="in_stock")
    await _offer(db, P + ":o:out", track="external_referral", tier="referral_only",
                 availability="out_of_stock")

    report = await _run(db)

    assert report["availability"]["planned"] == 1
    assert report["availability"]["updated"] == 1
    assert report["availability"]["vocabulary_verdict"]["low_stock"] == "unknown"

    rows = {r["offer_id"]: r["availability"] for r in [
        dict(x) for x in await db.fetch_all(
            "SELECT offer_id, availability FROM catalog_offers WHERE offer_id LIKE :p "
            "ORDER BY offer_id", {"p": P + "%"})]}
    assert rows[P + ":o:low"] == "unknown"
    assert rows[P + ":o:in"] == "in_stock"
    assert rows[P + ":o:out"] == "out_of_stock"


async def test_an_out_of_stock_spelling_is_repaired_to_out_of_stock_not_to_unknown(db):
    """`unknown` must not become a dumping ground. The vocabulary DOES recognise "Sold Out",
    and collapsing everything it cannot exact-match to `unknown` would resurrect sold-out
    products into a servable state — `unknown` is servable.

    The operator has to NAME that verdict, though: `out_of_stock` delists, so the default
    allowed set is `{unknown}` and this run passes the wider set on purpose (the refusal is
    pinned separately below)."""
    await _product(db, SEED_PRODUCT, "external_seed")
    await _offer(db, P + ":o:sold", track="external_referral", tier="referral_only",
                 availability="Sold Out")

    report = await _run(db, allowed_verdicts=frozenset({"unknown", "out_of_stock"}))

    assert report["availability"]["vocabulary_verdict"]["Sold Out"] == "out_of_stock"
    assert report["availability"]["apply_would_refuse"] == {}
    row = dict(await db.fetch_one(
        "SELECT availability FROM catalog_offers WHERE offer_id = :k", {"k": P + ":o:sold"}))
    assert row["availability"] == "out_of_stock"


async def test_apply_refuses_a_delisting_verdict_it_was_not_told_to_write_and_writes_nothing(db):
    """THE BLAST RADIUS IS THE VOCABULARY'S, NOT THE DRY RUN'S. The availability repair is
    lane-agnostic on purpose, and the vocabulary has three outcomes, so an `--apply` can write
    `out_of_stock` — a delisting on every surface that reads the column — on an INTERNAL
    merchant's checkout offer. The dry run prints the verdicts, but the apply re-reads the
    offending set from the database: a "Temporarily out of stock" that a crawler wrote between
    the reviewed dry run and the apply would be delisted under a verdict nobody read.

    So an apply refuses any verdict outside its allowed set, and it refuses BEFORE the first
    write: the readiness_tier repairs must be untouched too, and no audit row may exist,
    because a half-run with no audit row is the shape an operator cannot see."""
    import scripts.fix_external_track_vocabulary as fix

    await _product(db, SEED_PRODUCT, "external_seed")
    await _product(db, SHOPIFY_PRODUCT, "shopify")
    # A readiness_tier candidate on the external lane — the repair that runs FIRST in `run()`.
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready",
                 availability="low_stock")
    await _sku(db, P + ":s:ext", SEED_PRODUCT, "external_seed", "commerce_ready")
    # An internal checkout offer whose raw value the vocabulary resolves to a DELISTING.
    await _offer(db, P + ":o:internal", track="merchant_checkout", tier="commerce_ready",
                 availability="Temporarily out of stock", product_key=SHOPIFY_PRODUCT)

    # The dry run names what an apply with the default set would refuse.
    plan = await _run(db, apply=False)
    assert plan["availability"]["vocabulary_verdict"] == {
        "low_stock": "unknown", "Temporarily out of stock": "out_of_stock"}
    assert plan["availability"]["apply_would_refuse"] == {
        "Temporarily out of stock": "out_of_stock"}
    assert plan["availability"]["allowed_verdicts"] == ["unknown"]

    with pytest.raises(fix.RefusedVerdict) as exc:
        await _run(db)
    assert exc.value.refused == {"Temporarily out of stock": "out_of_stock"}
    assert "--allow-verdict out_of_stock" in str(exc.value)

    # Nothing was written — not the availability, and not the two repairs that run before it.
    assert await _tier(db, "catalog_offers", "offer_id", P + ":o:ext") == "commerce_ready"
    assert await _tier(db, "catalog_skus", "sku_key", P + ":s:ext") == "commerce_ready"
    rows = {r["offer_id"]: r["availability"] for r in [
        dict(x) for x in await db.fetch_all(
            "SELECT offer_id, availability FROM catalog_offers WHERE offer_id LIKE :p",
            {"p": P + "%"})]}
    assert rows[P + ":o:ext"] == "low_stock"
    assert rows[P + ":o:internal"] == "Temporarily out of stock"
    assert dict(await db.fetch_one(
        "SELECT count(*) AS n FROM writer_audit_log WHERE writer_name = :w",
        {"w": "fix_external_track_vocabulary"}))["n"] == 0

    # Named, the same run writes everything — and records the wider set on the audit row.
    applied = await _run(db, allowed_verdicts=frozenset({"unknown", "out_of_stock"}))
    assert applied["total_updated"] == 4
    assert applied["availability"]["apply_would_refuse"] == {}
    rows = {r["offer_id"]: r["availability"] for r in [
        dict(x) for x in await db.fetch_all(
            "SELECT offer_id, availability FROM catalog_offers WHERE offer_id LIKE :p",
            {"p": P + "%"})]}
    assert rows[P + ":o:ext"] == "unknown"
    assert rows[P + ":o:internal"] == "out_of_stock"


async def test_a_named_verdict_still_refuses_a_raw_value_the_dry_run_never_showed(db):
    """The verdict check is verdict-grain. Once `out_of_stock` is allowed, ANY raw value the
    vocabulary resolves to it is written — including `reserved` on 5,000 internal checkout
    offers that a crawler stored between the reviewed dry run and the apply. Naming the raw
    values the dry run showed pins the apply to them; an unnamed one refuses before any write.
    Not naming any keeps the pin off: the verdict check alone is the default guarantee."""
    import scripts.fix_external_track_vocabulary as fix

    await _product(db, SEED_PRODUCT, "external_seed")
    await _product(db, SHOPIFY_PRODUCT, "shopify")
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready",
                 availability="Sold Out")
    await _offer(db, P + ":o:internal", track="merchant_checkout", tier="commerce_ready",
                 availability="reserved", product_key=SHOPIFY_PRODUCT)
    wide = frozenset({"unknown", "out_of_stock"})

    plan = await _run(db, apply=False, allowed_verdicts=wide,
                      expected_raw_values=frozenset({"Sold Out"}))
    assert plan["availability"]["apply_would_refuse"] == {}
    assert plan["availability"]["apply_would_refuse_raw_values"] == {"reserved": "out_of_stock"}

    with pytest.raises(fix.RefusedRawValue) as exc:
        await _run(db, allowed_verdicts=wide, expected_raw_values=frozenset({"Sold Out"}))
    assert exc.value.unexpected == {"reserved": "out_of_stock"}
    assert await _tier(db, "catalog_offers", "offer_id", P + ":o:ext") == "commerce_ready"
    rows = {r["offer_id"]: r["availability"] for r in [
        dict(x) for x in await db.fetch_all(
            "SELECT offer_id, availability FROM catalog_offers WHERE offer_id LIKE :p",
            {"p": P + "%"})]}
    assert rows == {P + ":o:ext": "Sold Out", P + ":o:internal": "reserved"}
    assert dict(await db.fetch_one(
        "SELECT count(*) AS n FROM writer_audit_log WHERE writer_name = :w",
        {"w": "fix_external_track_vocabulary"}))["n"] == 0

    applied = await _run(db, allowed_verdicts=wide,
                         expected_raw_values=frozenset({"Sold Out", "reserved"}))
    assert applied["availability"]["updated"] == 2
    reasons = dict(await db.fetch_one(
        "SELECT reasons FROM writer_audit_log WHERE writer_name = :w",
        {"w": "fix_external_track_vocabulary"}))["reasons"]
    reasons = reasons if isinstance(reasons, dict) else json.loads(reasons)
    # The audit row records how WIDE the authorisation was, not only what was written.
    assert reasons["allowed_verdicts"] == ["out_of_stock", "unknown"]
    assert reasons["expected_raw_values"] == ["Sold Out", "reserved"]


def test_the_null_branch_writes_the_planned_verdict_not_a_literal():
    """The column is NOT NULL, so the branch is unreachable today — which is exactly why its
    target must be the planned verdict bound at run time: a literal would be the one write in
    the file that bypasses the allowed-set check, the day a migration relaxes the constraint."""
    import scripts.fix_external_track_vocabulary as fix

    assert ":target" in fix.UPDATE_AVAILABILITY_NULL_SQL
    assert "'unknown'" not in fix.UPDATE_AVAILABILITY_NULL_SQL
    assert fix.availability_repair_for(None) == "unknown"


async def test_a_resurrecting_verdict_is_refused_the_same_way(db):
    """`in_stock` is the other decision. The vocabulary does map `InStock` to it, and writing
    a positive availability builds carts, so it is named per run like the delisting is."""
    import scripts.fix_external_track_vocabulary as fix

    await _product(db, SHOPIFY_PRODUCT, "shopify")
    await _offer(db, P + ":o:internal", track="merchant_checkout", tier="commerce_ready",
                 availability="InStock", product_key=SHOPIFY_PRODUCT)

    with pytest.raises(fix.RefusedVerdict):
        await _run(db)
    row = dict(await db.fetch_one(
        "SELECT availability FROM catalog_offers WHERE offer_id = :k",
        {"k": P + ":o:internal"}))
    assert row["availability"] == "InStock"

    applied = await _run(db, allowed_verdicts=frozenset({"unknown", "in_stock"}))
    assert applied["availability"]["updated"] == 1


# --- dry run, idempotency, audit --------------------------------------------------------------


async def test_the_dry_run_writes_nothing_and_plans_exactly_what_apply_moves(db):
    """The COUNT and the UPDATE carry the same predicate written twice. Nothing syntactic keeps
    them equal, so the equality is asserted on behaviour: plan a run, prove it wrote nothing,
    then apply and require the same numbers."""
    await _product(db, SEED_PRODUCT, "external_seed")
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready",
                 availability="low_stock")
    await _sku(db, P + ":s:ext", SEED_PRODUCT, "external_seed", "commerce_ready")

    plan = await _run(db, apply=False)
    assert plan["mode"] == "dry_run"
    assert plan["total_updated"] == 0
    assert await _tier(db, "catalog_offers", "offer_id", P + ":o:ext") == "commerce_ready"
    assert await _tier(db, "catalog_skus", "sku_key", P + ":s:ext") == "commerce_ready"
    assert dict(await db.fetch_one(
        "SELECT count(*) AS n FROM writer_audit_log WHERE writer_name = :w",
        {"w": "fix_external_track_vocabulary"}))["n"] == 0

    applied = await _run(db, apply=True)
    assert applied["total_updated"] == plan["total_planned"]
    assert applied["offer_readiness_tier"]["updated"] == plan["offer_readiness_tier"]["planned"]
    assert applied["sku_readiness_tier"]["updated"] == plan["sku_readiness_tier"]["planned"]
    assert applied["availability"]["updated"] == plan["availability"]["planned"]


async def test_a_second_apply_is_a_no_op(db):
    """Idempotency is what makes this safe to re-run after the promoter's own fix lands. It
    holds because each repair's predicate selects the BROKEN state, which the first run
    removes — not because of any bookkeeping."""
    await _product(db, SEED_PRODUCT, "external_seed")
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready",
                 availability="low_stock")
    await _sku(db, P + ":s:ext", SEED_PRODUCT, "external_seed", "commerce_ready")

    first = await _run(db)
    assert first["total_updated"] == 3

    second = await _run(db)
    assert second["total_planned"] == 0
    assert second["total_updated"] == 0
    assert second["availability"]["vocabulary_verdict"] == {}


async def test_apply_writes_one_writer_audit_log_row_carrying_the_verdict(db):
    await _product(db, SEED_PRODUCT, "external_seed")
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready",
                 availability="low_stock")

    report = await _run(db)

    row = dict(await db.fetch_one(
        "SELECT batch_id, applied_rows, reasons FROM writer_audit_log "
        "WHERE writer_name = :w", {"w": "fix_external_track_vocabulary"}))
    assert row["batch_id"] == report["batch_id"]
    assert row["applied_rows"] == report["total_updated"]
    reasons = row["reasons"] if isinstance(row["reasons"], dict) else json.loads(row["reasons"])
    # The decision, durable. A dropped log line loses the stdout report; this row cannot be
    # dropped, and "what did we decide low_stock meant on the run that wrote" is the question.
    assert reasons["availability_vocabulary_verdict"]["low_stock"] == "unknown"


async def test_limit_bounds_each_repair_so_a_first_bite_can_be_small(db):
    await _product(db, SEED_PRODUCT, "external_seed")
    for i in range(4):
        await _offer(db, P + ":o:%d" % i, track="external_referral", tier="commerce_ready")

    report = await _run(db, limit=2, page=1)

    assert report["offer_readiness_tier"] == {"planned": 4, "updated": 2}


# --- the report mode, and the fenced one-line output -------------------------------------------


async def test_report_is_read_only_and_counts_the_sku_population_both_ways(db):
    await _product(db, SEED_PRODUCT, "external_seed")
    await _product(db, SHOPIFY_PRODUCT, "shopify")
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready",
                 availability="low_stock")
    await _sku(db, P + ":s:liar", SHOPIFY_PRODUCT, "external_seed", "commerce_ready")

    import scripts.fix_external_track_vocabulary as fix
    out = await fix.report(db=db)

    assert out["mode"] == "report"
    census = out["commerce_ready_skus_membership"]
    # The join's cost, in rows: the liar SKU is counted by the platform column and not by the
    # join, and the census says so instead of leaving the operator to infer it.
    assert census["by_sku_platform_column"] >= 1
    assert census["sku_platform_disagrees_with_product"] >= 1
    # read-only
    assert await _tier(db, "catalog_offers", "offer_id", P + ":o:ext") == "commerce_ready"

    tracks = {(r["readiness_tier"], r["catalog_track"]) for r in
              out["offers_by_readiness_tier_and_track"]}
    assert ("commerce_ready", "external_referral") in tracks
    assert "low_stock" in {r["availability"] for r in out["offers_by_availability"]}


async def test_the_cli_exits_2_on_a_refusal_and_reports_it_on_the_fenced_line(db):
    """`run_oneoff_job.sh` reads the container's exit code as the verdict. A refusal that
    exited 0 would be ticked off as a successful repair while the rows are still there, with
    the only trace a JSON key inside a Cloud Logging line. Driven as a subprocess against the
    same database, so what is asserted is what `main()` actually printed and returned."""
    await _product(db, SEED_PRODUCT, "external_seed")
    await _offer(db, P + ":o:ext", track="external_referral", tier="commerce_ready",
                 availability="Sold Out")

    proc = subprocess.run(
        [sys.executable, "-B", "scripts/fix_external_track_vocabulary.py", "--apply"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 2, (proc.stdout[-2000:], proc.stderr[-2000:])
    fenced = [ln for ln in proc.stdout.splitlines()
              if ln.startswith("VOCABREPORT>>>") and ln.endswith("<<<VOCABREPORT")]
    assert len(fenced) == 1, proc.stdout[-3000:]
    body = json.loads(fenced[0][len("VOCABREPORT>>>"):-len("<<<VOCABREPORT")])
    assert body["mode"] == "refused"
    assert body["refused_verdicts"] == {"Sold Out": "out_of_stock"}
    assert "--allow-verdict out_of_stock" in proc.stderr

    # And it wrote nothing — the tier repair that runs first included.
    assert await _tier(db, "catalog_offers", "offer_id", P + ":o:ext") == "commerce_ready"
    assert dict(await db.fetch_one(
        "SELECT count(*) AS n FROM writer_audit_log WHERE writer_name = :w",
        {"w": "fix_external_track_vocabulary"}))["n"] == 0


def test_the_cli_prints_the_report_on_exactly_one_fenced_line():
    """Driven as a SUBPROCESS, because the thing under test is `main()` — argparse, the fence,
    and the single line. `scripts/ops/run_oneoff_job.sh` reads this back out of Cloud Logging,
    which DROPS LINES: a multi-line report arrives with arbitrary keys missing and nothing
    saying anything is gone. An in-process assertion on `report()`'s dict cannot see that.
    """
    for mode in (["--report"], [], ["--allow-verdict", "out_of_stock"]):
        proc = subprocess.run(
            [sys.executable, "-B", "scripts/fix_external_track_vocabulary.py"] + mode,
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        fenced = [ln for ln in proc.stdout.splitlines()
                  if ln.startswith("VOCABREPORT>>>") and ln.endswith("<<<VOCABREPORT")]
        assert len(fenced) == 1, proc.stdout[-3000:]
        body = json.loads(fenced[0][len("VOCABREPORT>>>"):-len("<<<VOCABREPORT")])
        assert body["mode"] == ("report" if mode == ["--report"] else "dry_run")
        if mode == ["--allow-verdict", "out_of_stock"]:
            assert body["availability"]["allowed_verdicts"] == ["out_of_stock", "unknown"]

    # A verdict outside the vocabulary is an argparse error, not a silently widened set.
    proc = subprocess.run(
        [sys.executable, "-B", "scripts/fix_external_track_vocabulary.py",
         "--allow-verdict", "low_stock"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 2
    assert "invalid choice" in proc.stderr
