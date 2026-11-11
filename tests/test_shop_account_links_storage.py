"""Shop account-link storage (db/shop_account_links.py, migration 277): the constraints that keep a
revoked link from holding a token, one Shop account from landing on two Pivota accounts, and a
checkout from claiming an outcome it never had. The same tables on real Postgres, built from the
migration itself: tests/test_shop_account_links_storage_postgres.py."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, event, insert, update
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import shop_account_links as sal  # noqa: E402
from db.database import metadata  # noqa: E402
from db.sql_migrations import list_migration_files, needs_autocommit  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_MIG_277 = _REPO / "db" / "migrations" / "277_shop_account_links.sql"
_DOWN_277 = _REPO / "db" / "migrations" / "down" / "277_shop_account_links_down.sql"

TABLES = (sal.shop_account_claims, sal.shop_account_links, sal.shop_link_attempts, sal.shop_checkouts)
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
SUB = sal.sha256_hex("shop-sub-1")
CT = "tc1.k1.sealed"


@pytest.fixture
def conn():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _foreign_keys_on(dbapi_conn, _record):  # SQLite enforces foreign keys only when asked
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    metadata.create_all(engine, tables=list(TABLES))
    with engine.begin() as connection:
        connection.execute(insert(sal.shop_account_claims).values(shop_sub_hash=SUB, pivota_account_id="acct_1"))
        yield connection


def _rejected(conn, table, values):
    savepoint = conn.begin_nested()
    with pytest.raises(IntegrityError):
        conn.execute(insert(table).values(**values))
    savepoint.rollback()


def _link(**overrides):
    row = {
        "link_id": "lnk_1",
        "pivota_account_id": "acct_1",
        "agent_client_id": "cli_1",
        "shop_sub_hash": SUB,
        "claimed_sub_hash": SUB,
        "scope": "openid personal_agent",
        "refresh_token_ct": CT,
        "status": "active",
    }
    row.update(overrides)
    return row


_REVOKED = {"status": "revoked", "refresh_token_ct": None, "claimed_sub_hash": None, "revoked_at": NOW}


def _attempt(**overrides):
    row = {
        "attempt_id": "att_1",
        "ticket_hash": sal.sha256_hex("ticket"),
        "pivota_account_id": "acct_1",
        "agent_client_id": "cli_1",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    row.update(overrides)
    return row


def _checkout(**overrides):
    row = {
        "checkout_id_hash": sal.sha256_hex("gid://shop/Checkout/1"),
        "pivota_account_id": "acct_1",
        "agent_client_id": "cli_1",
        "shop_domain": "example.myshopify.com",
        "market": "US",
    }
    row.update(overrides)
    return row


# ---- registration ---------------------------------------------------------------------------------


def test_every_table_is_registered_but_main_does_not_import_the_module_yet():
    """Prod gets tables from create_all, which never ALTERs. Registering before the first writer
    would freeze this shape in prod; the first writer PR adds the import (module docstring)."""
    for table in TABLES:
        assert metadata.tables[table.name] is table
    # Import main in a FRESH interpreter and look at what got loaded. A string check on main.py
    # misses `from db import shop_account_links`, `from db.shop_account_links import ...` and any
    # transitive import through a module main imports, and each of those registers the tables.
    import subprocess

    probe = "import sys, main; print('db.shop_account_links' in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=_REPO, env=dict(os.environ), capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "False"


def test_no_column_could_hold_a_raw_checkout_id_continue_url_or_access_token():
    names = {c.name for t in TABLES for c in t.columns}
    for forbidden in ("checkout_id", "continue_url", "access_token", "refresh_token", "code_verifier", "id_token"):
        assert forbidden not in names, forbidden


# ---- shop_account_claims ----------------------------------------------------------------------------


def test_one_shop_account_belongs_to_one_pivota_account(conn):
    _rejected(conn, sal.shop_account_claims, {"shop_sub_hash": SUB, "pivota_account_id": "acct_2"})


def test_a_live_link_needs_its_own_accounts_claim(conn):
    """The race the claim exists for: account 2 cannot hold a live link to a Shop account that
    account 1 has claimed, and nobody can hold one with no claim at all."""
    other = sal.sha256_hex("unclaimed-sub")

    _rejected(conn, sal.shop_account_links, _link(pivota_account_id="acct_2"))
    _rejected(conn, sal.shop_account_links, _link(shop_sub_hash=other, claimed_sub_hash=other))


def test_a_claim_cannot_be_deleted_under_a_live_link_but_goes_with_the_last_one(conn):
    conn.execute(insert(sal.shop_account_links).values(**_link()))
    release = delete(sal.shop_account_claims).where(sal.shop_account_claims.c.shop_sub_hash == SUB)

    savepoint = conn.begin_nested()
    with pytest.raises(IntegrityError):
        conn.execute(release)
    savepoint.rollback()

    conn.execute(update(sal.shop_account_links).values(**_REVOKED))
    assert conn.execute(release).rowcount == 1


def test_a_claim_stores_a_hash_not_the_sub(conn):
    _rejected(conn, sal.shop_account_claims, {"shop_sub_hash": "shop-sub-1", "pivota_account_id": "acct_1"})


# ---- shop_account_links ------------------------------------------------------------------------------


def test_an_active_link_is_stored(conn):
    conn.execute(insert(sal.shop_account_links).values(**_link()))


def test_one_live_link_per_account_and_client(conn):
    table = sal.shop_account_links
    conn.execute(insert(table).values(**_link()))

    _rejected(conn, table, _link(link_id="lnk_2"))
    _rejected(conn, table, _link(link_id="lnk_2", status="needs_reconnect"))
    # Another agent client of the same account connects on its own (O5).
    conn.execute(insert(table).values(**_link(link_id="lnk_3", agent_client_id="cli_2")))


def test_a_revoked_link_frees_the_slot_for_a_reconnect(conn):
    table = sal.shop_account_links
    conn.execute(insert(table).values(**_link(**_REVOKED)))

    conn.execute(insert(table).values(**_link(link_id="lnk_2")))


@pytest.mark.parametrize(
    "case, overrides",
    [
        ("active without a token", {"refresh_token_ct": None}),
        ("revoked still holding a token", {**_REVOKED, "refresh_token_ct": CT}),
        ("revoked without revoked_at", {**_REVOKED, "revoked_at": None}),
        ("revoked still holding its claim", {**_REVOKED, "claimed_sub_hash": SUB}),
        ("revoked_at on a live link", {"revoked_at": NOW}),
        ("a live link without its claim", {"claimed_sub_hash": None}),
        ("a live link claiming another sub", {"claimed_sub_hash": sal.sha256_hex("other")}),
        ("an unknown status", {"status": "paused"}),
        ("generation zero", {"link_generation": 0}),
        ("a raw sub instead of its hash", {"shop_sub_hash": "shop-sub-1", "claimed_sub_hash": "shop-sub-1"}),
    ],
)
def test_a_link_that_breaks_its_rules_is_refused(conn, case, overrides):
    _rejected(conn, sal.shop_account_links, _link(**overrides))


def test_a_new_link_starts_at_generation_one(conn):
    conn.execute(insert(sal.shop_account_links).values(**_link()))

    generation = conn.execute(sal.shop_account_links.select()).mappings().one()["link_generation"]

    assert generation == 1


# ---- shop_link_attempts --------------------------------------------------------------------------------


def test_a_fresh_attempt_has_only_its_ticket(conn):
    conn.execute(insert(sal.shop_link_attempts).values(**_attempt()))


def test_connect_sets_state_verifier_nonce_and_click_together(conn):
    table = sal.shop_link_attempts
    full = {
        "state_hash": sal.sha256_hex("state"),
        "code_verifier_ct": CT,
        "nonce_hash": sal.sha256_hex("nonce"),
        "connect_clicked_at": NOW,
    }
    conn.execute(insert(table).values(**_attempt(**full)))

    for missing in full:
        partial = {k: v for k, v in full.items() if k != missing}
        _rejected(conn, table, _attempt(attempt_id=f"att_{missing}", ticket_hash=sal.sha256_hex(missing), **partial))


def test_tickets_and_states_are_single_use(conn):
    table = sal.shop_link_attempts
    state = {
        "state_hash": sal.sha256_hex("state"),
        "code_verifier_ct": CT,
        "nonce_hash": sal.sha256_hex("nonce"),
        "connect_clicked_at": NOW,
    }
    conn.execute(insert(table).values(**_attempt(**state)))

    _rejected(conn, table, _attempt(attempt_id="att_2"))
    _rejected(conn, table, _attempt(attempt_id="att_3", ticket_hash=sal.sha256_hex("other"), **state))


def test_an_attempt_must_expire_after_it_was_created(conn):
    _rejected(conn, sal.shop_link_attempts, _attempt(expires_at=NOW))


@pytest.mark.parametrize("column", ["ticket_hash", "state_hash", "nonce_hash"])
def test_attempt_hash_columns_hold_hashes(conn, column):
    _rejected(conn, sal.shop_link_attempts, _attempt(**{column: "raw-value"}))


# ---- shop_checkouts ------------------------------------------------------------------------------------


def test_a_new_checkout_is_open(conn):
    conn.execute(insert(sal.shop_checkouts).values(**_checkout()))

    assert conn.execute(sal.shop_checkouts.select()).mappings().one()["outcome"] == "open"


@pytest.mark.parametrize(
    "case, overrides",
    [
        ("an attempt without a mandate", {"completion_attempted_at": NOW}),
        ("a decline without an attempt", {"outcome": "declined"}),
        ("an unknown outcome without an attempt", {"outcome": "unknown_outcome"}),
        ("completed without an order", {"outcome": "completed"}),
        ("an unknown outcome name", {"outcome": "paid"}),
        ("a raw checkout id instead of its hash", {"checkout_id_hash": "gid://shop/Checkout/1"}),
        ("a raw mandate instead of its hash", {"mandate_hash": "mandate-1"}),
        ("a raw order ref instead of its hash", {"outcome": "completed", "order_ref_hash": "#1001"}),
    ],
)
def test_a_checkout_that_claims_what_it_never_had_is_refused(conn, case, overrides):
    _rejected(conn, sal.shop_checkouts, _checkout(**overrides))


def test_a_handoff_the_buyer_paid_is_completed_with_its_order(conn):
    conn.execute(
        insert(sal.shop_checkouts).values(**_checkout(outcome="completed", order_ref_hash=sal.sha256_hex("order")))
    )


def test_one_row_per_checkout_so_a_second_completion_has_nowhere_to_go(conn):
    table = sal.shop_checkouts
    conn.execute(insert(table).values(**_checkout()))
    _rejected(conn, table, _checkout(agent_client_id="cli_2"))

    # The writer's guard: only the first UPDATE that sets the attempt wins.
    claim = (
        update(table)
        .where(table.c.checkout_id_hash == _checkout()["checkout_id_hash"])
        .where(table.c.completion_attempted_at.is_(None))
        .values(completion_attempted_at=NOW, mandate_hash=sal.sha256_hex("mandate"))
    )
    assert conn.execute(claim).rowcount == 1
    assert conn.execute(claim).rowcount == 0


# ---- migration -----------------------------------------------------------------------------------------


def test_migration_277_runs_as_one_transaction_and_is_applied_by_the_runner():
    body = _MIG_277.read_text()
    assert needs_autocommit(body) is False
    assert str(_MIG_277) in list_migration_files()
    assert {fk.column.table.name for t in TABLES for fk in t.foreign_keys} <= {t.name for t in TABLES}


def test_migration_277_declares_every_model_column_constraint_and_index():
    """Text parity. The real-Postgres file builds both and compares the catalogs."""
    body = _MIG_277.read_text()
    for table in TABLES:
        match = re.search(rf"CREATE TABLE IF NOT EXISTS {table.name} \((.*?)\n\);", body, re.S)
        assert match, table.name
        block = match.group(1)
        for column in table.columns:
            assert re.search(rf"^\s+{column.name}\s", block, re.M), f"{table.name}.{column.name}"
        for constraint in table.constraints:
            if constraint.name and constraint.name.startswith(("ck_", "uq_", "fk_")):
                assert f"CONSTRAINT {constraint.name}" in block, constraint.name
        for index in table.indexes:
            assert f"INDEX IF NOT EXISTS {index.name} ON {table.name}" in body.replace("\n  ON", " ON"), index.name


def test_the_down_file_drops_exactly_these_tables():
    dropped = re.findall(r"DROP TABLE IF EXISTS (\w+);", _DOWN_277.read_text())

    assert sorted(dropped) == sorted(t.name for t in TABLES)


def test_the_aad_helpers_name_the_row_and_its_owner():
    a = sal.refresh_token_aad(link_id="l1", pivota_account_id="acct_1", agent_client_id="cli_1")
    assert a != sal.refresh_token_aad(link_id="l1", pivota_account_id="acct_2", agent_client_id="cli_1")
    assert a != sal.refresh_token_aad(link_id="l1", pivota_account_id="acct_1", agent_client_id="cli_2")
    assert a != sal.refresh_token_aad(link_id="l2", pivota_account_id="acct_1", agent_client_id="cli_1")
    assert sal.code_verifier_aad(attempt_id="l1", pivota_account_id="acct_1", agent_client_id="cli_1") != a


def test_the_aad_context_is_unambiguous_when_an_id_contains_a_colon():
    """With a ":" join, ("X:Y", "Z") and ("X", "Y:Z") sealed under the same context."""
    a = sal.refresh_token_aad(link_id="X:Y", pivota_account_id="Z", agent_client_id="W")
    b = sal.refresh_token_aad(link_id="X", pivota_account_id="Y:Z", agent_client_id="W")
    assert a != b
    c = sal.code_verifier_aad(attempt_id='a"],["b', pivota_account_id="Z", agent_client_id="W")
    assert c != sal.code_verifier_aad(attempt_id="a", pivota_account_id="b", agent_client_id="Z")


@pytest.mark.parametrize("bad", ["", None])
def test_an_empty_aad_part_is_refused(bad):
    with pytest.raises(ValueError):
        sal.refresh_token_aad(link_id=bad, pivota_account_id="acct_1", agent_client_id="cli_1")


def test_a_live_link_cannot_borrow_another_shop_accounts_claim(conn):
    """The FK alone is satisfied here: (hash("other"), acct_1) IS a claim. Only
    ck_shop_account_links_live_is_claimed (claimed_sub_hash = shop_sub_hash) stops a link for SUB
    riding on another Shop account's claim, which would bypass one-account-per-Shop-account for SUB."""
    other = sal.sha256_hex("other")
    conn.execute(insert(sal.shop_account_claims).values(shop_sub_hash=other, pivota_account_id="acct_1"))

    _rejected(conn, sal.shop_account_links, _link(claimed_sub_hash=other))
