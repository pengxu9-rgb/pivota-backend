"""The stamping backfill's write path, EXECUTED — not asserted about.

Every semantic this script depends on is Postgres-only and unreachable from the SQLite suite:

  * `COALESCE(sku_payload, '{}'::jsonb) || jsonb_build_object(...)` — jsonb concatenation, and
    the variadic-`"any"` type inference that makes the CASTs load-bearing (#1703);
  * `jsonb_typeof`, and what `||` does when one side is not an object;
  * `RETURNING` as the only way to learn whether an UPDATE matched, because `databases` on
    asyncpg returns no rowcount from `execute()`;
  * a nested `database.transaction()` behaving as a SAVEPOINT, which is the entire reason one
    bad row does not cost its page.

A string assertion cannot tell a correct statement from a plausible-looking wrong one, and the
sibling file `tests/test_backfill_variant_identity_skus_postgres.py` records what happened the
last time this lane relied on one: five fixes were reverted one at a time against a green
27-test baseline and ALL FIVE survived.

Named `test_*_postgres.py` so the dialect gate's glob picks it up with no ride-along edit
(`.github/workflows/postgres-dialect-gate.yml`).

🚨 THESE GATE FILES SHARE ONE DATABASE. Tables come from the real `db/` models via
`ensure_model_tables`; nothing here drops a table, and teardown is a scoped DELETE.
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

MERCHANT = "m_stamp_pg"
PLATFORM = "external_seed"
#: The keyspace is chosen to sort LAST. The scan this file drives has a lower bound (`_scoped`)
#: and no upper one, so any catalog_skus row sorting after the cursor is scanned too — and
#: `tests/test_recall_per_product_sku_cap_postgres.py` leaves `pk_other_*` rows behind, which sort
#: after every `ext:`/`prod::`/`m_` prefix the other gate files use. Under the dialect gate's glob
#: order this file happens to run first, so an `ext:` prefix would be green in CI and red for
#: anyone running the files in another order — the "free rides on the sibling that sorts earlier"
#: failure, inverted. A `zzz:` prefix removes the dependence on order instead of relying on it.
PK = "zzz:stamp-lipstick::deadbeef"
SPID = "stamp-lipstick"

#: One id per class the classifier can return, in the shapes prod actually holds.
MERCHANT_VID = "43062643884185"          # bare numeric Shopify id, 14 digits
DERIVED_DEFAULT_VID = SPID + "-default"  # onboard_external_brand_from_crawl's synthetic
DERIVED_RESTATED_VID = PK                # ingestion.py's `source_variant_id = product_key`
UNVERIFIABLE_VID = "ruby-woo-30ml"       # a handle: present, unplaceable

#: Immediately below PK, and below nothing else in the table.
SCAN_FLOOR = "zzz:"

SOURCE_SYSTEM = "variant_id_provenance_stamp_v1"
WRITER_NAME = "backfill_variant_id_provenance_stamps"

#: The CLI tests at the bottom of this file run the script as a real subprocess, from here.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _ddl(database):
    """Build from the REAL models. Never drop; never hand-write DDL for a db.catalog table."""
    from db.catalog import catalog_skus, writer_audit_log
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables([catalog_skus, writer_audit_log])


async def _clear(database):
    await database.execute(
        "DELETE FROM catalog_skus WHERE product_key LIKE :p", {"p": PK + "%"}
    )
    await database.execute(
        "DELETE FROM writer_audit_log WHERE writer_name = :w", {"w": WRITER_NAME}
    )


@pytest.fixture
async def db():
    from db.database import database

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _ddl(database)
    await _clear(database)
    # The scan has a lower bound (`_scoped`) and no upper one, so a row LEFT BEHIND by a sibling
    # gate file whose sku_key sorts after ours would be scanned and silently inflate every count
    # this file asserts on. Nothing sorts there today, and every gate file cleans up after
    # itself — but a crashed neighbour is exactly the case where a confusing count mismatch is
    # the worst possible message. Fail with the reason instead.
    residue = await database.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE sku_key > :after", {"after": SCAN_FLOOR}
    )
    assert residue == 0, (
        f"{residue} catalog_skus row(s) sort after this file's keyset cursor and would be "
        "scanned by its runs. The shared dialect-gate database has residue from another file; "
        "these counts would be wrong rather than merely different."
    )
    try:
        yield database
    finally:
        await _clear(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _sku(database, *, vid, payload="absent", product_key=PK, sku_key=None,
               platform=PLATFORM, suppressed=None):
    """Insert one catalog_skus row.

    `payload="absent"` writes SQL NULL; anything else is written as jsonb verbatim, so a test can
    plant an array or a scalar as easily as an object.
    """
    key = sku_key or f"{product_key}::v:{vid}"
    await database.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, sku_payload, suppressed_at,
             created_at, updated_at)
           VALUES (:sk,:pk,:m,:pl,:spid,:vid,'Stamp Test',
                   CAST(:payload AS jsonb),:sup,NOW(),NOW())""",
        {"sk": key, "pk": product_key, "m": MERCHANT, "pl": platform, "spid": SPID,
         "vid": vid, "payload": None if payload == "absent" else json.dumps(payload),
         "sup": suppressed},
    )
    return key


async def _payload(database, sku_key):
    raw = await database.fetch_val(
        "SELECT sku_payload FROM catalog_skus WHERE sku_key = :k", {"k": sku_key}
    )
    return json.loads(raw) if isinstance(raw, str) else raw


async def _run(**kw):
    import scripts.backfill_variant_id_provenance_stamps as stamps

    return await stamps.run(**{"apply": True, "limit": 0, "after": "", "page": 100, **kw})


def _scoped(**kw):
    """Confine a run to THIS fixture's rows.

    The dialect gate's database is shared and other files leave catalog_skus rows behind; an
    unscoped scan would stamp them and make counts depend on collection order. `after` is the
    keyset cursor, and every key here starts with PK — which sorts above everything any other
    gate file writes, so the scan's missing upper bound costs nothing (see PK).
    """
    return {"after": SCAN_FLOOR, **kw}


# ---------------------------------------------------------------------------
# each class, stamped correctly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vid,expected",
    [
        (MERCHANT_VID, "merchant_issued"),
        (DERIVED_DEFAULT_VID, "product_derived"),
        (DERIVED_RESTATED_VID, "product_derived"),
        (UNVERIFIABLE_VID, "unverifiable"),
    ],
    ids=["numeric_shopify", "epid_default", "product_key_restated", "handle"],
)
async def test_it_stamps_each_class_the_classifier_returns(db, vid, expected):
    key = await _sku(db, vid=vid)
    report = await _run(**_scoped())

    assert report["stamped"] == 1
    payload = await _payload(db, key)
    assert payload["variant_id_provenance"] == expected
    assert payload["provenance_stamped_by"] == SOURCE_SYSTEM
    assert report["class_stamped_this_run"] == {expected: 1}


async def test_the_stamp_matches_the_classifier_rather_than_this_files_expectations(db):
    """The stamp must come FROM `services.variant_identity`, not from a rule re-derived here.

    A backfill that re-implements the predicate is the defect the classifier module exists to
    prevent — so this asserts equality with the imported function's own answer, on all four ids
    at once, rather than against the literals above (which the parametrize above already pins).
    """
    from services.variant_identity import variant_id_provenance

    keys = {
        vid: await _sku(db, vid=vid)
        for vid in (MERCHANT_VID, DERIVED_DEFAULT_VID, DERIVED_RESTATED_VID, UNVERIFIABLE_VID)
    }
    await _run(**_scoped())
    for vid, key in keys.items():
        payload = await _payload(db, key)
        assert payload["variant_id_provenance"] == variant_id_provenance(
            vid, product_id=SPID, product_key=PK
        ), vid


# ---------------------------------------------------------------------------
# what it must never touch
# ---------------------------------------------------------------------------


async def test_an_already_stamped_row_is_left_exactly_as_it_was(db):
    key = await _sku(
        db, vid=MERCHANT_VID,
        payload={"variant_id_provenance": "merchant_issued", "agent_version": "promoter_v1"},
    )
    report = await _run(**_scoped())

    assert report["stamped"] == 0
    assert report["already_stamped_agree"] == 1
    assert report["already_stamped_disagree"] == 0
    payload = await _payload(db, key)
    assert payload == {"variant_id_provenance": "merchant_issued", "agent_version": "promoter_v1"}
    assert "provenance_stamped_by" not in payload


async def test_a_disagreeing_stamp_is_counted_and_never_overwritten(db):
    """The row says merchant_issued; the classifier says product_derived. The stamp stands.

    Overwriting here would launder a finding into agreement — and it is the one case where the
    script's `IS NULL` predicate is doing work that a `stamped != computed` predicate would undo.
    """
    key = await _sku(
        db, vid=DERIVED_RESTATED_VID,
        payload={"variant_id_provenance": "merchant_issued"},
    )
    report = await _run(**_scoped())

    assert report["already_stamped_disagree"] == 1
    assert report["stamped"] == 0
    assert (await _payload(db, key))["variant_id_provenance"] == "merchant_issued"
    sample = report["disagreement_sample"]
    assert len(sample) == 1
    assert sample[0]["sku_key"] == key
    assert sample[0]["stored"] == "merchant_issued"
    assert sample[0]["classifier"] == "product_derived"


async def test_a_suppressed_row_is_stamped_because_provenance_is_not_a_serving_decision(db):
    import datetime as dt

    key = await _sku(
        db, vid=MERCHANT_VID,
        suppressed=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    report = await _run(**_scoped())

    assert report["stamped"] == 1
    assert (await _payload(db, key))["variant_id_provenance"] == "merchant_issued"


# ---------------------------------------------------------------------------
# payload shapes
# ---------------------------------------------------------------------------


async def test_a_null_sku_payload_becomes_an_object_rather_than_staying_null(db):
    """`NULL || jsonb` is NULL. Without the COALESCE the UPDATE would erase, not stamp — and
    the column is nullable, so the day a row is NULL the failure is invisible."""
    key = await _sku(db, vid=MERCHANT_VID, payload="absent")
    assert await db.fetch_val(
        "SELECT sku_payload IS NULL FROM catalog_skus WHERE sku_key = :k", {"k": key}
    ) is True

    report = await _run(**_scoped())
    assert report["stamped"] == 1
    assert await _payload(db, key) == {
        "variant_id_provenance": "merchant_issued",
        "provenance_stamped_by": SOURCE_SYSTEM,
    }


async def test_the_merge_keeps_every_key_the_previous_writer_left(db):
    key = await _sku(
        db, vid=MERCHANT_VID,
        payload={"agent_version": "promoter_v1", "source_handle": "ruby-woo", "variant_id": "x"},
    )
    await _run(**_scoped())
    payload = await _payload(db, key)
    assert payload["agent_version"] == "promoter_v1"
    assert payload["source_handle"] == "ruby-woo"
    assert payload["variant_id"] == "x"
    assert payload["variant_id_provenance"] == "merchant_issued"


async def test_a_non_object_payload_is_refused_and_counted_not_errored_through(db):
    """`'[]'::jsonb || '{...}'::jsonb` raises. Refusing in SQL and in Python keeps that out of
    the row-error path, where it would read as an unexplained failure."""
    key = await _sku(db, vid=MERCHANT_VID, payload=[1, 2])
    report = await _run(**_scoped())

    assert report["skipped_payload_not_object"] == 1
    assert report["stamped"] == 0
    assert report["row_errors"] == 0
    assert await _payload(db, key) == [1, 2]


# ---------------------------------------------------------------------------
# paging, limit, idempotency
# ---------------------------------------------------------------------------


async def test_a_product_whose_rows_straddle_a_page_boundary_loses_none(db):
    """The keyset cursor must be the PRIMARY KEY, not `product_key`.

    Nine rows share one product_key. Paged on `product_key > :after` with page=4, the first page
    ends mid-product and the next page skips every remaining row of it — silently, with the
    report still claiming it scanned what it stamped. Paged on `sku_key`, all nine are stamped.
    """
    keys = [await _sku(db, vid=f"{MERCHANT_VID}{i}") for i in range(9)]
    report = await _run(page=4, **_scoped())

    assert report["rows_scanned"] == 9
    assert report["stamped"] == 9
    stamped = await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk"
        "   AND sku_payload->>'variant_id_provenance' IS NOT NULL",
        {"pk": PK},
    )
    assert stamped == 9


async def test_running_it_twice_stamps_nothing_the_second_time(db):
    for i in range(4):
        await _sku(db, vid=f"{MERCHANT_VID}{i}")
    first = await _run(**_scoped())
    second = await _run(**_scoped())

    assert first["stamped"] == 4
    assert second["stamped"] == 0
    assert second["rows_already_stamped"] == 4
    assert second["already_stamped_agree"] == 4
    assert second["already_stamped_disagree"] == 0


async def test_limit_bounds_the_rows_stamped_not_the_rows_scanned(db):
    """A pilot wants N WRITES, and `resume_after` must point at the last row it looked at — not
    at the last row its final page returned, which would skip the remainder of that page.

    THE TWO ROWS THAT ARE ALREADY STAMPED ARE THE POINT. With every seeded row unstamped,
    `rows_scanned` and `stamped` are the same number, and a mutant bounding the SCAN instead of
    the WRITES produces identical output — it survived. Stamped rows are scanned and not
    written, so the two counts separate (5 scanned, 3 stamped) and only the correct predicate
    reports both.
    """
    for i in range(2):        # sort first: already carry a stamp, must be scanned past
        await _sku(db, vid=f"{MERCHANT_VID}{i}",
                   payload={"variant_id_provenance": "merchant_issued"})
    for i in range(2, 9):     # seven unstamped rows, of which the pilot may write only three
        await _sku(db, vid=f"{MERCHANT_VID}{i}")

    pilot = await _run(limit=3, page=5, **_scoped())
    assert pilot["stamped"] == 3
    assert pilot["stopped_at_limit"] == 1
    assert pilot["rows_scanned"] == 5, (
        "--limit must bound the rows STAMPED; scanning stopped at 3 rows, which is the "
        "scan-bounded reading of --limit"
    )
    assert pilot["rows_already_stamped"] == 2

    rest = await _run(after=pilot["resume_after"], page=5)
    assert rest["stamped"] == 4
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk"
        "   AND sku_payload->>'variant_id_provenance' IS NOT NULL",
        {"pk": PK},
    ) == 9


async def test_a_dry_run_writes_nothing_and_plans_what_apply_then_writes(db):
    for vid in (MERCHANT_VID, DERIVED_DEFAULT_VID, UNVERIFIABLE_VID):
        await _sku(db, vid=vid)

    dry = await _run(apply=False, **_scoped())
    assert dry["applied"] == 0
    assert dry["stamped"] == 3
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk"
        "   AND sku_payload IS NOT NULL", {"pk": PK}
    ) == 0
    assert await db.fetch_val(
        "SELECT count(*) FROM writer_audit_log WHERE writer_name = :w", {"w": WRITER_NAME}
    ) == 0

    live = await _run(**_scoped())
    assert live["stamped"] == dry["stamped"]
    assert live["class_stamped_this_run"] == dry["class_stamped_this_run"]


# ---------------------------------------------------------------------------
# the report and the audit row
# ---------------------------------------------------------------------------


async def test_the_report_is_one_line_and_survives_a_line_dropping_log(db):
    """Cloud Logging drops lines. A multi-line report arrives with arbitrary keys missing and
    nothing saying so, which is how a pilot read `skus: 78` with `offers` silently absent."""
    import scripts.backfill_variant_id_provenance_stamps as stamps

    await _sku(db, vid=MERCHANT_VID)
    report = await _run(**_scoped())
    line = stamps.REPORT_BEGIN + json.dumps(report, sort_keys=True, default=str) + stamps.REPORT_END

    assert "\n" not in line
    body = line[len(stamps.REPORT_BEGIN):-len(stamps.REPORT_END)]
    assert json.loads(body)["stamped"] == 1


async def test_an_applied_run_leaves_a_writer_audit_row_carrying_the_class_breakdown(db):
    for vid in (MERCHANT_VID, UNVERIFIABLE_VID):
        await _sku(db, vid=vid)
    report = await _run(**_scoped())

    row = dict(await db.fetch_one(
        "SELECT * FROM writer_audit_log WHERE writer_name = :w ORDER BY id DESC LIMIT 1",
        {"w": WRITER_NAME},
    ))
    assert row["batch_id"] == report["batch_id"]
    assert row["applied_rows"] == 2
    reasons = json.loads(row["reasons"]) if isinstance(row["reasons"], str) else row["reasons"]
    assert reasons["class_stamped_this_run"] == {"merchant_issued": 1, "unverifiable": 1}
    # Zeros reach the audit row explicitly: record_info drops <= 0, so without this key
    # "measured zero" and "never measured" would be indistinguishable there.
    assert "already_stamped_disagree" in reasons["zero_counters"]


async def test_the_report_carries_the_class_by_platform_identity_table(db):
    await _sku(db, vid=MERCHANT_VID, platform="external_seed")
    await _sku(db, vid=DERIVED_DEFAULT_VID, platform="shopify",
               sku_key=PK + "::v:shopify-derived")
    report = await _run(apply=False, **_scoped())

    assert report["class_by_platform"]["external_seed"] == {"merchant_issued": 1}
    assert report["class_by_platform"]["shopify"] == {"product_derived": 1}
    assert report["class_all"] == {"merchant_issued": 1, "product_derived": 1}


# ---------------------------------------------------------------------------
# the PREPARE gate, and the savepoint
# ---------------------------------------------------------------------------


def test_every_sql_constant_this_script_declares_is_registered_with_the_prepare_gate():
    """A statement that cannot be PLANNED writes nothing on every row (#1703).

    `jsonb_build_object` is variadic `"any"`, which is exactly the shape that failed there, so
    this script's registration in the ops PREPARE gate is not optional. The gate's own
    completeness guard then covers the NEXT constant added here, not just today's two.
    """
    import tests.test_ops_script_sql_prepare_postgres as gate

    script = "scripts/backfill_variant_id_provenance_stamps.py"
    assert script in gate._COVERED_SCRIPTS, (
        "the stamping backfill is not registered in the ops PREPARE gate; its "
        "jsonb_build_object binds would ship unplanned"
    )
    collected = " ".join(origin for origin, _ in gate._COVERED_SCRIPTS[script]())
    assert "SELECT_PAGE_SQL" in collected and "STAMP_SQL" in collected


async def test_the_updates_own_is_null_predicate_refuses_a_row_stamped_since_the_select(db):
    """TWO guards close this door and only ONE of them can be reached from a plain run.

    The Python scan skips any row whose SELECT showed a stamp, so the `IS NULL` in the UPDATE's
    WHERE never fires on ordinary input — which makes deleting it invisible to every other test
    in this file (it was: a mutant that removed it survived the whole suite). Its actual job is
    the RACE: an ingest that stamps the row between our read and our write. So the input here is
    built so that only the SQL predicate can catch it — the row is stamped, with a DIFFERENT
    value, after it has already been planned.
    """
    import scripts.backfill_variant_id_provenance_stamps as stamps

    key = await _sku(db, vid=MERCHANT_VID)          # classifier says merchant_issued
    real_fetch_val = stamps.database.fetch_val

    async def _racing(query, values=None):
        if values and "provenance" in values:
            await db.execute(
                "UPDATE catalog_skus SET sku_payload = CAST(:p AS jsonb) WHERE sku_key = :k",
                {"k": values["sku_key"],
                 "p": json.dumps({"variant_id_provenance": "unverifiable"})},
            )
        return await real_fetch_val(query, values)

    stamps.database.fetch_val = _racing
    try:
        report = await _run(**_scoped())
    finally:
        stamps.database.fetch_val = real_fetch_val

    assert report["raced_already_stamped"] == 1
    assert report["stamped"] == 0
    # The other writer's value stands. Overwriting it would be the same laundering the
    # disagreement test forbids, only under a race instead of a scan.
    assert (await _payload(db, key)) == {"variant_id_provenance": "unverifiable"}


async def test_the_update_also_refuses_a_payload_that_stopped_being_an_object(db):
    """The second half of the same argument: `jsonb || jsonb` demands two objects, and the
    Python `payload_type` check cannot see a payload that changes shape after the SELECT.
    Without the SQL guard this row raises and is counted as an unexplained `row_errors`."""
    import scripts.backfill_variant_id_provenance_stamps as stamps

    key = await _sku(db, vid=MERCHANT_VID)
    real_fetch_val = stamps.database.fetch_val

    async def _racing(query, values=None):
        if values and "provenance" in values:
            await db.execute(
                "UPDATE catalog_skus SET sku_payload = CAST(:p AS jsonb) WHERE sku_key = :k",
                {"k": values["sku_key"], "p": json.dumps([1, 2])},
            )
        return await real_fetch_val(query, values)

    stamps.database.fetch_val = _racing
    try:
        report = await _run(**_scoped())
    finally:
        stamps.database.fetch_val = real_fetch_val

    assert report["raced_already_stamped"] == 1
    assert report["row_errors"] == 0
    assert report["stamped"] == 0
    assert await _payload(db, key) == [1, 2]


async def test_a_page_that_rolls_back_counts_nothing_and_names_its_own_resume_point(db):
    """Counting beside the UPDATE instead of after the COMMIT is the failure this pins.

    A page whose transaction fails has written nothing, but its rows all returned a sku_key from
    RETURNING moments earlier — so a report built from those returns says `stamped: 4` for four
    rows that do not exist. And because the scan must move past the page to make progress,
    `resume_after` is already beyond them: the run must hand back the cursor the LOST page began
    at, or recovering 4 rows means re-scanning the table.
    """
    import scripts.backfill_variant_id_provenance_stamps as stamps

    keys = [await _sku(db, vid=f"{MERCHANT_VID}{i}") for i in range(4)]
    real_transaction = stamps.database.transaction
    depth = {"n": 0}

    def _factory(*args, **kwargs):
        is_outer = depth["n"] == 0
        inner = real_transaction(*args, **kwargs)

        class _Wrapped:
            async def __aenter__(self):
                depth["n"] += 1
                return await inner.__aenter__()

            async def __aexit__(self, exc_type, exc, tb):
                depth["n"] -= 1
                if is_outer and exc_type is None:
                    boom = RuntimeError("simulated commit failure")
                    await inner.__aexit__(RuntimeError, boom, None)   # a REAL rollback
                    raise boom
                return await inner.__aexit__(exc_type, exc, tb)

        return _Wrapped()

    stamps.database.transaction = _factory
    try:
        report = await _run(page=10, **_scoped())
    finally:
        stamps.database.transaction = real_transaction

    assert report["page_rollbacks"] == 1
    assert report["rows_lost_to_page_rollback"] == 4
    assert report["stamped"] == 0
    assert report["class_stamped_this_run"] == {}
    for key in keys:
        assert await _payload(db, key) is None
    # resume_after is past the lost page by construction; this key is what recovers it.
    assert report["resume_after_to_recover_rollbacks"] == SCAN_FLOOR
    assert report["resume_after"] != report["resume_after_to_recover_rollbacks"]


async def test_one_bad_row_rolls_back_to_its_savepoint_and_its_page_still_commits(db):
    """The per-row `database.transaction()` must be a SAVEPOINT, not decoration.

    Proven by making ONE row's UPDATE fail — the statement is patched to reference a column that
    does not exist, for a single sku_key — and checking the other rows of the same page are
    committed. Without the savepoint, the failed statement aborts the page transaction and all
    four rows are lost while the report still says the page ran.
    """
    import scripts.backfill_variant_id_provenance_stamps as stamps

    keys = [await _sku(db, vid=f"{MERCHANT_VID}{i}") for i in range(4)]
    doomed = keys[1]

    real_fetch_val = stamps.database.fetch_val

    async def _poisoned(query, values=None):
        if values and values.get("sku_key") == doomed:
            return await real_fetch_val(
                "SELECT no_such_column FROM catalog_skus WHERE sku_key = :sku_key",
                {"sku_key": values["sku_key"]},
            )
        return await real_fetch_val(query, values)

    stamps.database.fetch_val = _poisoned
    try:
        report = await _run(page=10, **_scoped())
    finally:
        stamps.database.fetch_val = real_fetch_val

    assert report["row_errors"] == 1
    assert report["stamped"] == 3
    assert report["page_rollbacks"] == 0
    for key in keys:
        payload = await _payload(db, key)
        if key == doomed:
            assert payload is None
        else:
            assert payload["variant_id_provenance"] == "merchant_issued"


# ---------------------------------------------------------------------------
# the resume cursor, and what a mid-run failure leaves behind
# ---------------------------------------------------------------------------


async def test_the_audit_row_and_the_resume_cursor_survive_an_error_mid_run(db):
    """An exception used to lose the report, the resume cursor AND the audit row.

    `run()` sealed the report into a return value it then discarded with `raise`, `main()`
    never reached its print, and `audit.reasons` never carried `resume_after` — so the pages
    that HAD committed were recoverable only by re-scanning 30,000 rows to find the ~500 left.
    Modelled on `tests/test_backfill_variant_identity_skus_postgres.py`'s F3.

    The failure lands on the THIRD page, so there are committed pages behind it and unscanned
    rows ahead of it: the only shape where the cursor is worth anything.
    """
    import scripts.backfill_variant_id_provenance_stamps as stamps

    keys = [await _sku(db, vid=f"{MERCHANT_VID}{i}") for i in range(9)]
    real_fetch_all = stamps.database.fetch_all
    pages = {"n": 0}

    async def _dies_on_the_third_page(query, values=None):
        pages["n"] += 1
        if pages["n"] == 3:
            raise RuntimeError("simulated mid-run failure")
        return await real_fetch_all(query, values)

    stamps.database.fetch_all = _dies_on_the_third_page
    try:
        with pytest.raises(RuntimeError) as caught:
            await _run(page=3, **_scoped())
    finally:
        stamps.database.fetch_all = real_fetch_all

    assert pages["n"] == 3

    # 1. The rows two committed pages wrote are durable — the failure did not undo them.
    for key in keys[:6]:
        assert (await _payload(db, key))["variant_id_provenance"] == "merchant_issued", key
    for key in keys[6:]:
        assert await _payload(db, key) is None, key

    # 2. The report rode out ON THE EXCEPTION, because `raise` discards a return value.
    report = getattr(caught.value, "stamp_report", None)
    assert isinstance(report, dict), "the sealed report did not reach the caller"
    assert report["mode"] == "failed"
    assert report["mode_attempted"] == "apply"
    assert "simulated mid-run failure" in report["error"]
    assert report["stamped"] == 6, "only durably committed rows may be reported as stamped"
    assert report["resume_after"] == keys[5]

    # 3. And the audit row carries the cursor too. stdout goes to Cloud Logging, which drops
    #    lines; this is the copy that is still there tomorrow.
    row = dict(await db.fetch_one(
        "SELECT * FROM writer_audit_log WHERE writer_name = :w ORDER BY id DESC LIMIT 1",
        {"w": WRITER_NAME},
    ))
    assert row["applied_rows"] == 6
    reasons = json.loads(row["reasons"]) if isinstance(row["reasons"], str) else row["reasons"]
    assert reasons["resume_after"] == keys[5]
    assert "simulated mid-run failure" in reasons["run_failed"]

    # 4. Resuming from it finishes the job, touching nothing already done.
    rest = await _run(after=report["resume_after"], page=10)
    assert rest["stamped"] == 3
    assert rest["rows_already_stamped"] == 0


async def test_a_failure_to_seal_the_report_does_not_replace_the_original_exception(db):
    """The seal runs INSIDE the failure path, so it can fail too — and if the run died because
    the connection went away, the audit INSERT dies the same way. Reporting the write error
    instead of the cause would point the operator at the wrong thing entirely."""
    import scripts.backfill_variant_id_provenance_stamps as stamps

    await _sku(db, vid=MERCHANT_VID)
    real_fetch_all = stamps.database.fetch_all
    real_write = stamps.write_writer_audit_log

    async def _boom(query, values=None):
        raise RuntimeError("the original cause")

    async def _seal_also_fails(audit):
        raise RuntimeError("the audit insert failed too")

    stamps.database.fetch_all = _boom
    stamps.write_writer_audit_log = _seal_also_fails
    try:
        with pytest.raises(RuntimeError) as caught:
            await _run(**_scoped())
    finally:
        stamps.database.fetch_all = real_fetch_all
        stamps.write_writer_audit_log = real_write

    assert "the original cause" in str(caught.value)
    assert "audit insert" not in str(caught.value)


async def test_a_keyset_cursor_that_stops_advancing_raises_instead_of_spinning_forever(db):
    """Note 1 of the module docstring says paging with `>=` "loops forever". Nothing made that
    loud, and a silent infinite loop is the worst failure this script has: no error, no output,
    a report that never prints, and a Cloud Run job burning until someone kills it.

    This does not describe the mutant — it EXECUTES it, swapping the real `>` for `>=`. The
    fetch counter is what makes a regression FAIL rather than hang the gate: without the guard
    the `while True` would call `fetch_all` forever, and the bound turns that into a red test.
    """
    import scripts.backfill_variant_id_provenance_stamps as stamps

    for i in range(9):
        await _sku(db, vid=f"{MERCHANT_VID}{i}")

    real_sql = stamps.SELECT_PAGE_SQL
    real_fetch_all = stamps.database.fetch_all
    calls = {"n": 0}

    async def _bounded(query, values=None):
        calls["n"] += 1
        if calls["n"] > 40:
            raise AssertionError(
                "the scan is spinning: the keyset progress guard did not fire and this run "
                "would never terminate"
            )
        return await real_fetch_all(query, values)

    stamps.SELECT_PAGE_SQL = real_sql.replace("sku_key > :after", "sku_key >= :after")
    stamps.database.fetch_all = _bounded
    try:
        with pytest.raises(RuntimeError, match="did not advance"):
            await _run(page=4, **_scoped())
    finally:
        stamps.SELECT_PAGE_SQL = real_sql
        stamps.database.fetch_all = real_fetch_all

    assert calls["n"] <= 40


# ---------------------------------------------------------------------------
# the handle divergence, measured on the rows this run writes
# ---------------------------------------------------------------------------


async def test_a_handle_only_in_the_payload_is_measured_on_unstamped_rows_never_stamped(db):
    """The divergence from `ingestion.py` reaches rows THIS SCRIPT WRITES, not only rows that
    are already stamped.

    The docstring used to justify omitting the handle by saying it only appears on rows
    ingestion wrote, "which are already stamped". False: ingestion began writing
    `source_handle` on 2026-09-04 (8c9c1f26e) and `variant_id_provenance` on 2026-09-08
    (d466bc6ee), so four days of rows carry a handle and no stamp — exactly this population.

    What is stamped stays the no-handle answer, because that is the question the money reader
    `services/checkout_preflight.py:189` asks. The difference is COUNTED instead.
    """
    # numeric id + numeric handle it restates by a 3-digit ordinal: merchant_issued without
    # the handle, product_derived with it. The `parent + a small ordinal` case
    # `services/variant_identity._is_restatement_of` documents.
    money = await _sku(
        db, vid="43062643884185",
        payload={"source_handle": "43062643884"},
        sku_key=PK + "::v:handle-money",
    )
    # unverifiable without the handle, product_derived with it. Differs, but not FROM
    # merchant_issued — so it must move only the broader counter.
    quiet = await _sku(
        db, vid="ruby-woo-2",
        payload={"source_handle": "ruby-woo"},
        sku_key=PK + "::v:handle-quiet",
    )
    # carries a handle that changes nothing: must not be counted at all. A DIFFERENT numeric id
    # from `money`'s — `idx_catalog_skus_source_identity_v2` is unique on
    # (merchant_id, platform, product_key, source_variant_id), so two rows cannot share one.
    same = await _sku(
        db, vid="43062643884186",
        payload={"source_handle": "ruby-woo"},
        sku_key=PK + "::v:handle-same",
    )

    report = await _run(**_scoped())

    assert report["stamped"] == 3
    assert report["stamped_would_differ_with_handle"] == 2, "both diverging rows must count"
    assert report["stamped_would_differ_with_handle_from_merchant_issued"] == 1, (
        "only the row we stamp merchant_issued is the stop condition; the unverifiable one "
        "cannot over-promise at a checkout"
    )

    # The stamp itself is the no-handle answer on every one of them.
    assert (await _payload(db, money))["variant_id_provenance"] == "merchant_issued"
    assert (await _payload(db, quiet))["variant_id_provenance"] == "unverifiable"
    assert (await _payload(db, same))["variant_id_provenance"] == "merchant_issued"
    # and the handle each row carried is still there.
    assert (await _payload(db, money))["source_handle"] == "43062643884"


async def test_the_disagreement_sample_is_capped_while_its_count_stays_exact(db):
    """The report must survive Cloud Logging, which truncates a long line — so the sample is
    bounded and the COUNT is not. Nothing pinned the bound: every other test seeds one
    disagreement, so a mutant removing the cap check carried all of them and stayed green.
    """
    import scripts.backfill_variant_id_provenance_stamps as stamps

    over = stamps.DISAGREEMENT_SAMPLE_CAP + 7
    for i in range(over):
        await _sku(
            # `product_key` + a small ordinal: product_derived, and a DISTINCT id per row —
            # `idx_catalog_skus_source_identity_v2` is unique on
            # (merchant_id, platform, product_key, source_variant_id).
            db, vid=f"{DERIVED_RESTATED_VID}-{i}",                   # classifier: product_derived
            payload={"variant_id_provenance": "merchant_issued"},   # stored: disagrees
            sku_key=f"{PK}::v:disagree-{i:03d}",
        )

    report = await _run(**_scoped())

    assert report["already_stamped_disagree"] == over, "the count is exact regardless of the cap"
    assert len(report["disagreement_sample"]) == stamps.DISAGREEMENT_SAMPLE_CAP
    assert report["stamped"] == 0


async def test_a_stamp_does_not_touch_updated_at(db):
    """`updated_at = NOW()` here would be a FALSE freshness signal on ~22,000 rows.

    `services/merchant_catalog_listing_fallback_service.py:55-67` takes
    `GREATEST(o.updated_at, s.updated_at, p.updated_at)`, and
    `services/merchant_commerce_readiness_service.py:173-181,232` turns it into a seven-day
    clock behind the `catalog_freshness_stale` blocker — a stale catalog would be reported
    fresh. `services/pivot_query_service.py:1541-1551` sorts recall candidates by
    `sku_updated_at DESC` under a per-product cap AND a LIMIT, where the sort key decides which
    SKUs are served at all. A provenance stamp is metadata about the id string; it is not a
    change to the thing being sold, and must not claim to be one.

    Every sibling backfill bumps this column, so the omission looks like a mistake and would be
    "fixed" by the next reader. This is why it is not.
    """
    key = await _sku(db, vid=MERCHANT_VID)
    before = await db.fetch_val(
        "SELECT updated_at FROM catalog_skus WHERE sku_key = :k", {"k": key}
    )

    report = await _run(**_scoped())
    assert report["stamped"] == 1
    assert (await _payload(db, key))["variant_id_provenance"] == "merchant_issued"

    after = await db.fetch_val(
        "SELECT updated_at FROM catalog_skus WHERE sku_key = :k", {"k": key}
    )
    assert after == before, (
        "the stamp moved catalog_skus.updated_at; that clears the catalog_freshness_stale "
        "blocker and flattens the recall tie-break"
    )


# ---------------------------------------------------------------------------
# main() itself — nothing above this line executes the CLI
# ---------------------------------------------------------------------------


def _cli(*args, expect_code=0):
    """Drive the script AS THE OPERATOR DOES: a real process, real stdout, real exit code.

    In-process is not an option for the run path — `main()` calls `asyncio.run()`, which
    refuses to nest inside the loop these async tests already run in. And the fenced-line
    assertion is only worth anything if it reads what `main()` ACTUALLY PRINTED: a test that
    rebuilds the line from a report dict tests the test, and passes against a `main` that
    pretty-prints (`tests/test_backfill_variant_identity_skus.py` records that exact escape).
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-B", "scripts/backfill_variant_id_provenance_stamps.py", *args],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ},
    )
    assert proc.returncode == expect_code, (
        f"exit {proc.returncode} (wanted {expect_code}) for {args}\n"
        f"--- stdout ---\n{proc.stdout[-2000:]}\n--- stderr ---\n{proc.stderr[-3000:]}"
    )
    return proc.stdout


def _one_fenced_report(out):
    """Exactly one fenced line, parsed from stdout — the whole contract at once.

    A `json.dumps(..., indent=2)` mutant fails HERE and only here: the report spans many lines,
    the line carrying REPORT_BEGIN is a bare `STAMPREPORT>>>{`, and it neither ends with the
    closing fence nor parses. Nothing else in this file can see that.
    """
    import scripts.backfill_variant_id_provenance_stamps as stamps

    fenced = [ln for ln in out.splitlines() if stamps.REPORT_BEGIN in ln]
    assert len(fenced) == 1, f"{len(fenced)} fenced lines in stdout, wanted 1: {out[-2000:]!r}"
    line = fenced[0]
    assert line.startswith(stamps.REPORT_BEGIN), f"the fence does not open the line: {line!r}"
    assert line.endswith(stamps.REPORT_END), (
        "the report does not close on the line it opened on — a multi-line report arrives "
        f"from Cloud Logging with arbitrary keys silently missing: {line!r}"
    )
    return json.loads(line[len(stamps.REPORT_BEGIN):-len(stamps.REPORT_END)])


def test_the_cli_refuses_an_apply_that_cannot_prove_which_build_answered_it():
    """`run_oneoff_job.sh` runs `backend:latest` and nothing about the typed command says which
    build answered it. These three refusals are argparse-level, so they happen before any
    connection — which is why this one test needs no database."""
    import scripts.backfill_variant_id_provenance_stamps as stamps

    for argv in (
        ["--apply"],                                              # no token at all
        ["--apply", "--expect-contract", "stamp-v0-something"],   # a token from another build
        ["--apply", "--expect-contract", ""],                     # empty is not "unset"
    ):
        with pytest.raises(SystemExit) as caught:
            stamps.main(argv)
        assert caught.value.code == 2, argv

    # --report is read-only by definition; silently honouring --apply beside it would write
    # under a flag whose help says it does not.
    with pytest.raises(SystemExit) as caught:
        stamps.main(["--report", "--apply", "--expect-contract", stamps.CONTRACT])
    assert caught.value.code == 2

    # and the token that IS the contract is accepted by the parser (it gets as far as the run).
    assert stamps.CONTRACT == "stamp-v1-sku-key-cursor"


async def test_main_prints_one_fenced_report_per_mode_and_report_ignores_limit(db):
    """Every mode, driven through `main()`, asserting on what the process actually printed."""
    await _sku(db, vid=MERCHANT_VID)
    await _sku(db, vid=UNVERIFIABLE_VID)

    dry = _one_fenced_report(_cli("--after", SCAN_FLOOR))
    assert dry["mode"] == "dry_run"
    assert dry["applied"] == 0
    assert dry["stamped"] == 2
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk AND sku_payload IS NOT NULL",
        {"pk": PK},
    ) == 0, "a dry run through main() wrote something"

    # --report is read-only AND ignores --limit: it exists to census the whole table, so a
    # --limit that quietly bounded it would report a partial census as a complete one.
    out = _cli("--report", "--after", SCAN_FLOOR, "--limit", "1")
    rep = _one_fenced_report(out)
    assert rep["mode"] == "report"
    assert rep["applied"] == 0
    assert rep["stamped"] == 2, "--report honoured --limit"
    assert rep["stopped_at_limit"] == 0
    # the human table prints beside the machine line, never instead of it
    assert "variant id provenance x platform" in out

    live = _one_fenced_report(
        _cli("--apply", "--expect-contract", "stamp-v1-sku-key-cursor", "--after", SCAN_FLOOR)
    )
    assert live["mode"] == "apply"
    assert live["applied"] == 1
    assert live["stamped"] == 2
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk"
        "   AND sku_payload->>'variant_id_provenance' IS NOT NULL",
        {"pk": PK},
    ) == 2

    # --limit DOES bound a real run, so the flag is not inert — only --report ignores it.
    await _clear(db)
    for i in range(4):
        await _sku(db, vid=f"{MERCHANT_VID}{i}")
    pilot = _one_fenced_report(_cli("--after", SCAN_FLOOR, "--limit", "2"))
    assert pilot["stamped"] == 2
    assert pilot["stopped_at_limit"] == 1


async def test_main_still_prints_a_fenced_report_when_the_run_dies(db):
    """A failed run's `resume_after` is the number that makes recovery cheap, and printing it
    is the only way the operator who must type it ever sees it. Before this, a mid-run failure
    produced a traceback and nothing else.

    Driven by pointing the script at a DATABASE_URL that cannot resolve, so the failure is real
    rather than injected — main() must still fence a report and still exit non-zero.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-B", "scripts/backfill_variant_id_provenance_stamps.py",
         "--after", SCAN_FLOOR],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ,
             "DATABASE_URL": "postgresql://pgtest@127.0.0.1:1/no_such_database_at_all"},
    )
    assert proc.returncode != 0, "a failed run must still be a failed job"
    report = _one_fenced_report(proc.stdout)
    assert report["mode"] == "failed"
    assert report["error"]
