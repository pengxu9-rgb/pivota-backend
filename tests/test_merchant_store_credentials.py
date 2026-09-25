"""The shared credential-blob merge, driven against a REAL sqlite row.

`merchant_stores.api_key` is one cell holding several unrelated secrets and
pieces of derived state. Every platform's connect, provisioning and sweep writes
through this one function, so its two properties have to be pinned somewhere
that is not a route test with the merge stubbed out: with the helper always
stubbed, replacing its whole body with `credentials = dict(updates)` — the exact
overwrite it exists to avoid — survives an entire suite.

What CANNOT be observed here is serialization: sqlite has no `FOR UPDATE` and
`databases` serializes everything onto one connection. That claim is pinned in
`tests/test_webflow_ledger_postgres.py` and
`tests/test_squarespace_ledger_postgres.py`, with genuinely separate backends.
"""

from __future__ import annotations

import json

import pytest

STORE_ID = "store-x"
MERCHANT_ID = "merchant-x"


async def _stores(tmp_path, name: str):
    """A real `merchant_stores` table on aiosqlite.

    Raw DDL rather than a metadata table because `merchant_stores` is raw SQL in
    this repo; only the columns the merge reads and writes are here, so a merge
    that touched a column it should not would fail loudly rather than silently.
    """
    import databases

    db = databases.Database(f"sqlite+aiosqlite:///{tmp_path / name}.sqlite3")
    await db.connect()
    await db.execute(
        """
        CREATE TABLE merchant_stores (
            store_id TEXT PRIMARY KEY,
            merchant_id TEXT,
            platform TEXT,
            domain TEXT,
            name TEXT,
            api_key TEXT,
            status TEXT,
            last_sync TIMESTAMP,
            connected_at TIMESTAMP
        )
        """
    )
    return db


async def _seed(db, blob, *, platform="webflow", status=None):
    await db.execute(
        "INSERT INTO merchant_stores"
        " (store_id, merchant_id, platform, api_key, status)"
        " VALUES (:store_id, :merchant_id, :platform, :api_key, :status)",
        {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "platform": platform,
            "api_key": json.dumps(blob),
            "status": status,
        },
    )


async def _stored(db):
    row = await db.fetch_one(
        "SELECT api_key FROM merchant_stores WHERE store_id = :s", {"s": STORE_ID}
    )
    return json.loads(dict(row)["api_key"])


# ---- the codec --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"api_token": "t", "site_id": "s"}', {"api_token": "t", "site_id": "s"}),
        ({"api_token": "t"}, {"api_token": "t"}),
        # A bare string is the shape the column held before any platform's
        # telemetry existed. Reading it as credential-less would make a
        # reconnect look like a fresh connect.
        ("plain-token", {"api_key": "plain-token"}),
        # Unparseable JSON is read the same way rather than raising: a merge
        # that blew up on a malformed cell could never repair it.
        ("{not json", {"api_key": "{not json"}),
        ("[1,2,3]", {"api_key": "[1,2,3]"}),
        ("", {}),
        (None, {}),
    ],
)
def test_the_blob_codec_never_loses_a_row(raw, expected):
    from services.merchant_store_credentials import parse_store_credentials

    assert parse_store_credentials(raw) == expected


def test_the_bare_string_key_is_the_platforms_own_credential_field():
    """A string read into a key the read path never looks at is
    indistinguishable from no credential at all, so the fallback key is the
    caller's to name."""
    from services.merchant_store_credentials import parse_store_credentials
    from services.webflow_connection import (
        parse_webflow_credentials,
        webflow_read_tokens,
    )

    assert parse_store_credentials("t", bare_key="api_token") == {"api_token": "t"}
    assert parse_webflow_credentials("wf-token") == {"api_token": "wf-token"}
    assert webflow_read_tokens(parse_webflow_credentials("wf-token")) == ["wf-token"]


def test_serialize_round_trips():
    from services.merchant_store_credentials import (
        parse_store_credentials,
        serialize_store_credentials,
    )

    blob = {"api_token": "t", "reconciliation": {"orders": {"cursor": "x"}}}
    assert parse_store_credentials(serialize_store_credentials(blob)) == blob


# ---- the merge --------------------------------------------------------------


async def test_the_merge_preserves_every_key_it_was_not_asked_to_change(tmp_path):
    """The mutant this kills: `credentials = dict(updates)`.

    A sweep persists only `reconciliation`. If that write is an overwrite, the
    URL secret goes with it — and that secret is baked into the webhook URL
    registered AT WEBFLOW, so losing it leaves Webflow delivering to a path the
    receiver can only answer 401 to until someone re-provisions.
    """
    from services.merchant_store_credentials import merge_store_credentials

    db = await _stores(tmp_path, "preserve")
    try:
        await _seed(
            db,
            {
                "api_token": "live-token",
                "site_id": "site-1",
                "url_secret": "the-only-copy",
                "webhook_ids": {"ecomm_new_order": "wh-1"},
                "reconciliation": {"orders": {"cursor": "2026-09-01T00:00:00.000Z"}},
            },
        )

        persisted = await merge_store_credentials(
            store_id=STORE_ID,
            updates={"reconciliation": {"orders": {"cursor": "2026-09-05T00:00:00.000Z"}}},
            db=db,
        )

        assert persisted["reconciliation"] == {
            "orders": {"cursor": "2026-09-05T00:00:00.000Z"}
        }
        assert persisted["url_secret"] == "the-only-copy"
        assert persisted["api_token"] == "live-token"
        assert persisted["site_id"] == "site-1"
        assert persisted["webhook_ids"] == {"ecomm_new_order": "wh-1"}
        # And the return value is a genuine RE-READ of the row, not the dict the
        # caller handed in: `databases` + asyncpg reports no rowcount from an
        # UPDATE, so the re-read is the only proof the write actually landed.
        assert await _stored(db) == persisted
    finally:
        await db.disconnect()


async def test_the_merge_runs_its_mutate_against_the_STORED_blob(tmp_path):
    """`mutate` is how a connect drops the old site's keys inside the merge's own
    critical section. It must see what is STORED, not an empty dict — a mutate
    handed a blank blob would find nothing to drop and quietly preserve the stale
    credential it exists to remove."""
    from services.merchant_store_credentials import merge_store_credentials

    db = await _stores(tmp_path, "mutate")
    try:
        await _seed(
            db,
            {
                "api_token": "OLD-token",
                "site_id": "site-OLD",
                "url_secret": "stale",
                "reconciliation": {"orders": {"cursor": "x"}},
            },
        )
        seen = {}

        def _mutate(blob):
            seen.update(blob)
            for key in ("api_token", "site_id", "url_secret", "reconciliation"):
                blob.pop(key, None)
            return blob

        persisted = await merge_store_credentials(
            store_id=STORE_ID,
            mutate=_mutate,
            updates={"api_token": "NEW-token", "site_id": "site-NEW"},
            mark_connected=True,
            db=db,
        )

        assert seen["api_token"] == "OLD-token", "mutate must receive the STORED blob"
        assert persisted == {"api_token": "NEW-token", "site_id": "site-NEW"}
        assert "url_secret" not in persisted
        assert "reconciliation" not in persisted

        # `mark_connected` is part of the same statement, so a reconnect is one
        # write rather than a merge racing a status UPDATE.
        row = dict(
            await db.fetch_one(
                "SELECT status, connected_at FROM merchant_stores WHERE store_id = :s",
                {"s": STORE_ID},
            )
        )
        assert row["status"] == "active"
        assert row["connected_at"] is not None
    finally:
        await db.disconnect()


async def test_updates_win_over_the_mutates_view(tmp_path):
    """The order matters: `mutate` expresses "drop these", `updates` expresses
    "and this is the new value". Applying them the other way round would let a
    drop erase the replacement."""
    from services.merchant_store_credentials import merge_store_credentials

    db = await _stores(tmp_path, "order")
    try:
        await _seed(db, {"api_token": "OLD"})

        persisted = await merge_store_credentials(
            store_id=STORE_ID,
            mutate=lambda blob: {**blob, "api_token": "FROM-MUTATE"},
            updates={"api_token": "FROM-UPDATES"},
            db=db,
        )

        assert persisted["api_token"] == "FROM-UPDATES"
    finally:
        await db.disconnect()


async def test_the_merge_leaves_the_row_alone_when_not_marking_connected(tmp_path):
    """The negative counterpart: an ordinary cursor write must not resurrect a
    store an operator disabled, nor forge a `connected_at`."""
    from services.merchant_store_credentials import merge_store_credentials

    db = await _stores(tmp_path, "nomark")
    try:
        await _seed(db, {"api_token": "t"}, status="disabled")

        await merge_store_credentials(
            store_id=STORE_ID, updates={"reconciliation": {}}, db=db
        )

        row = dict(
            await db.fetch_one(
                "SELECT status, connected_at FROM merchant_stores WHERE store_id = :s",
                {"s": STORE_ID},
            )
        )
        assert row["status"] == "disabled"
        assert row["connected_at"] is None
    finally:
        await db.disconnect()


async def test_the_whole_cycle_runs_inside_one_transaction(tmp_path):
    """Read, mutate, write and re-read are ONE critical section.

    On Postgres that transaction is what carries the `SELECT ... FOR UPDATE` row
    lock. A merge that opened no transaction would still pass every assertion
    above and lose an update under concurrency.
    """
    from services.merchant_store_credentials import merge_store_credentials

    db = await _stores(tmp_path, "txn")
    opened = []
    real_transaction = db.transaction

    def _spy(*args, **kwargs):
        opened.append(True)
        return real_transaction(*args, **kwargs)

    db.transaction = _spy
    try:
        await _seed(db, {"api_token": "t"})
        await merge_store_credentials(
            store_id=STORE_ID, updates={"site_id": "s"}, db=db
        )
        assert opened == [True]
    finally:
        db.transaction = real_transaction
        await db.disconnect()


async def test_a_missing_row_writes_nothing_and_reads_back_empty(tmp_path):
    """An UPDATE against no row is a no-op, and the re-read says so rather than
    handing back the dict the caller passed in."""
    from services.merchant_store_credentials import merge_store_credentials

    db = await _stores(tmp_path, "missing")
    try:
        persisted = await merge_store_credentials(
            store_id="nope", updates={"api_token": "t"}, db=db
        )
        assert persisted == {}
    finally:
        await db.disconnect()


# ---- the Squarespace delegate ----------------------------------------------


async def test_the_squarespace_helper_is_this_function_with_its_own_codec(tmp_path):
    """Squarespace's helper was generalized into this one rather than copied.

    Its behaviour must be unchanged — every Squarespace caller and every
    Squarespace test still goes through `merge_squarespace_credentials`, and a
    behavioural drift here would show up as a Squarespace regression rather than
    as a Webflow one.
    """
    from services.squarespace_connection import merge_squarespace_credentials

    db = await _stores(tmp_path, "squarespace")
    try:
        await _seed(
            db,
            {
                "api_key": "sq-key",
                "website_id": "site-1",
                "webhook_secret": "shown-once",
            },
            platform="squarespace",
        )

        persisted = await merge_squarespace_credentials(
            store_id=STORE_ID,
            updates={"reconciliation": {"orders_cursor": "2026-09-05T00:00:00.000Z"}},
            db=db,
        )

        assert persisted["webhook_secret"] == "shown-once"
        assert persisted["api_key"] == "sq-key"
        assert persisted["reconciliation"] == {
            "orders_cursor": "2026-09-05T00:00:00.000Z"
        }
    finally:
        await db.disconnect()
