"""db/merchant_agent_checkout_facts and the census job's `--store`: the stored ADR-030 D11 fact.

Three layers, each against the real thing below it:

  * the store, on the process database (SQLite here; the same cases run on Postgres through
    tests/test_merchant_agent_checkout_facts_postgres.py, which star-imports this module);
  * `census.checkout_check`, fed rows that the job's own `build_row` produced from the
    specifications' example documents, never hand-written rows;
  * the job end to end (`amain` / `main` with `--store`) over a fake network and the real store.

No socket is opened and no name is resolved. Every row a test ages is aged with the DATABASE's
clock (a relative UPDATE), never with a pinned Python datetime.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from db import merchant_agent_checkout_facts as facts
from db.database import IS_POSTGRES, database
from jobs import merchant_protocol_profile_census as census
from utils import database_readiness
from tests.test_merchant_protocol_profile_census import (
    ACP_FULL,
    ACP_MIN,
    UCP_0825,
    UCP_EMPTY,
    A,
    Net,
    R,
    Run,
    U,
    _json,
    _raw,
)

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = _REPO / "db/migrations/294_merchant_agent_checkout_facts.sql"
_DOWN = _REPO / "db/migrations/down/294_merchant_agent_checkout_facts_down.sql"
TABLE = facts.TABLE

# A path answer is True (offers checkout), False (answered cleanly without it) or None (could not
# be read). These four leave the other path unread, so each test moves one thing at a time.
POSITIVE_UCP = facts.Check(facts.PUBLISHES, "ok", True, None, "2026-08-25")
POSITIVE_ACP = facts.Check(facts.PUBLISHES, "ok", None, True, None)
NEGATIVE = facts.Check(facts.DOES_NOT_PUBLISH, "ok", False, False)
UNREADABLE = facts.Check(facts.UNREADABLE, "ucp_timeout")

#: The SQLite copy of migration 294's table, for this suite only. Production's table is the
#: migration; nothing in the application builds one.
_CREATE_SQL_SQLITE = """
CREATE TABLE IF NOT EXISTS merchant_agent_checkout_facts (
    merchant_domain VARCHAR(255) PRIMARY KEY,
    checked_at TIMESTAMP NOT NULL,
    last_outcome VARCHAR(20) NOT NULL
        CONSTRAINT ck_merchant_agent_checkout_facts_outcome
        CHECK (last_outcome IN ('publishes', 'does_not_publish', 'unreadable')),
    last_code VARCHAR(64) NOT NULL,
    ucp_checkout_at TIMESTAMP,
    acp_checkout_at TIMESTAMP,
    negative_at TIMESTAMP,
    ucp_version VARCHAR(10),
    source VARCHAR(32) NOT NULL
)
"""


def _migration_statements() -> List[str]:
    """Migration 294 without its comment lines, one statement per entry."""
    body = "\n".join(line for line in _MIGRATION.read_text("utf-8").splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


#: This suite DROPS and empties its table, so on Postgres it runs only against a throwaway
#: database (the sibling gates' convention).
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_")


def _assert_throwaway_database() -> None:
    if not IS_POSTGRES:
        return
    url = (os.getenv("DATABASE_URL") or "").strip()
    dbname = url.rsplit("/", 1)[-1].split("?")[0]
    if not any(marker in dbname for marker in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop {TABLE} in database {dbname!r}: throwaway databases only")


async def _create_table() -> None:
    """Postgres: migration 294 itself, split the way the migration runner splits it. SQLite: the
    module's test table."""
    if IS_POSTGRES:
        from db.sql_migrations import split_statements

        for statement in split_statements(_MIGRATION.read_text("utf-8")):
            await database.execute(statement)
    else:
        await database.execute(_CREATE_SQL_SQLITE)


@pytest.fixture
async def _facts_db(monkeypatch):
    """The fact table, empty, on the process database. The table is this suite's alone: it is
    emptied before and after, and rebuilt afterwards for the test that drops it."""
    _assert_throwaway_database()
    monkeypatch.delenv(facts.MAX_AGE_ENV, raising=False)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _create_table()
    await database.execute(f"DELETE FROM {TABLE}")
    yield
    await _create_table()
    await database.execute(f"DELETE FROM {TABLE}")
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _age(domain: str, column: str, days: int) -> None:
    """Move one timestamp `days` into the past, by the database's own clock."""
    assert column in {"ucp_checkout_at", "acp_checkout_at", "negative_at", "checked_at"}
    if IS_POSTGRES:
        past = "CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) * INTERVAL '1 day')"
    else:
        past = "datetime('now', '-' || CAST(:days AS INTEGER) || ' days')"
    await database.execute(
        f"UPDATE {TABLE} SET {column} = {past} WHERE merchant_domain = :d", {"days": days, "d": domain})


async def _count() -> int:
    return int(await database.fetch_val(f"SELECT COUNT(*) FROM {TABLE}"))


# ── the store ────────────────────────────────────────────────────────────────────────────────


async def test_a_readable_profile_that_offers_checkout_meets_the_run_condition(_facts_db):
    assert await facts.record_check("brand-a.example", POSITIVE_UCP) is True
    assert await facts.meets_run_condition("brand-a.example") is True
    row = await facts.get_fact("brand-a.example")
    assert (row["last_outcome"], row["last_code"], row["ucp_version"], row["source"]) == (
        "publishes", "ok", "2026-08-25", "census")
    assert row["ucp_checkout_at"] is not None and row["acp_checkout_at"] is None and row["negative_at"] is None


async def test_a_merchant_nobody_checked_does_not_meet_it(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    assert await facts.meets_run_condition("brand-b.example") is False
    assert await facts.get_fact("brand-b.example") is None


@pytest.mark.parametrize("spelling", ["WWW.Brand-A.example", "https://www.brand-a.example/cart", "brand-a.example."])
async def test_the_row_is_read_under_any_spelling_of_the_domain(_facts_db, spelling):
    await facts.record_check("www.Brand-A.example", POSITIVE_UCP)
    assert await _count() == 1
    assert (await facts.get_fact("brand-a.example"))["merchant_domain"] == "brand-a.example"
    assert await facts.meets_run_condition(spelling) is True


@pytest.mark.parametrize("domain", [None, "", "   ", 7])
async def test_an_unusable_domain_is_never_written_and_never_met(_facts_db, domain):
    wrote = await facts.record_check(domain, POSITIVE_UCP)
    if domain == 7:  # "7" is a host name as far as the key rule goes: written under that key only
        assert wrote is True and await facts.meets_run_condition("brand-a.example") is False
    else:
        assert wrote is False and await _count() == 0
        assert await facts.meets_run_condition(domain) is False and await facts.get_fact(domain) is None


async def test_acp_checkout_alone_meets_it(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_ACP)
    row = await facts.get_fact("brand-a.example")
    assert row["acp_checkout_at"] is not None and row["ucp_checkout_at"] is None
    assert await facts.meets_run_condition("brand-a.example") is True


async def test_a_version_is_stored_only_with_a_ucp_profile_that_was_read(_facts_db):
    """On a first row as well: a check that did not read the UCP path stores no UCP version,
    whatever version the caller passed."""
    acp_only = facts.Check(facts.PUBLISHES, "ok", None, True, "2026-08-25")
    assert await facts.record_check("brand-a.example", acp_only) is True
    assert (await facts.get_fact("brand-a.example"))["ucp_version"] is None
    await facts.record_check("brand-b.example", POSITIVE_UCP)
    await facts.record_check("brand-b.example", acp_only._replace(ucp_version="2026-01-23"))
    assert (await facts.get_fact("brand-b.example"))["ucp_version"] == "2026-08-25", "kept, not replaced"


async def test_a_clean_negative_revokes_at_once(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await facts.record_check("brand-a.example", POSITIVE_ACP)
    await facts.record_check("brand-a.example", NEGATIVE)
    row = await facts.get_fact("brand-a.example")
    assert row["last_outcome"] == "does_not_publish" and row["negative_at"] is not None
    assert (row["ucp_checkout_at"], row["acp_checkout_at"], row["ucp_version"]) == (None, None, None)
    assert await facts.meets_run_condition("brand-a.example") is False


async def test_a_profile_that_stopped_listing_checkout_loses_its_positive_though_acp_is_walled(_facts_db):
    """Review case A. UCP listed checkout; a later run reads the profile WITHOUT it while the ACP
    path sits behind a bot wall. The check is unreadable overall (no negative), but the UCP
    positive must not stay fresh-looking for thirty days."""
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    walled = facts.Check(facts.UNREADABLE, "acp_403", False, None, "2026-11-01")
    assert await facts.record_check("brand-a.example", walled) is True
    row = await facts.get_fact("brand-a.example")
    assert (row["last_outcome"], row["ucp_checkout_at"], row["negative_at"]) == ("unreadable", None, None)
    assert row["ucp_version"] == "2026-11-01", "the readable profile's own version"
    assert await facts.meets_run_condition("brand-a.example") is False


async def test_one_paths_clean_no_leaves_the_other_paths_fresh_positive(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await facts.record_check("brand-a.example", POSITIVE_ACP)
    await facts.record_check("brand-a.example", facts.Check(facts.UNREADABLE, "acp_403", False, None))
    row = await facts.get_fact("brand-a.example")
    assert row["ucp_checkout_at"] is None and row["acp_checkout_at"] is not None
    assert await facts.meets_run_condition("brand-a.example") is True
    await facts.record_check("brand-a.example", facts.Check(facts.UNREADABLE, "ucp_403", None, False))
    assert (await facts.get_fact("brand-a.example"))["acp_checkout_at"] is None
    assert await facts.meets_run_condition("brand-a.example") is False


async def test_a_positive_on_one_path_clears_the_other_paths_stale_positive(_facts_db):
    """Review case B. UCP positive, then both gone, then only ACP publishes: no UCP timestamp or
    version may survive to look fresh."""
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await facts.record_check("brand-a.example", NEGATIVE)
    await facts.record_check("brand-a.example", facts.Check(facts.PUBLISHES, "ok", False, True))
    row = await facts.get_fact("brand-a.example")
    assert (row["ucp_checkout_at"], row["ucp_version"], row["negative_at"]) == (None, None, None)
    assert row["acp_checkout_at"] is not None and await facts.meets_run_condition("brand-a.example") is True
    await facts.record_check("brand-b.example", POSITIVE_ACP)
    await facts.record_check("brand-b.example", facts.Check(facts.PUBLISHES, "ok", True, False, "2026-08-25"))
    row = await facts.get_fact("brand-b.example")
    assert row["acp_checkout_at"] is None and row["ucp_checkout_at"] is not None


async def test_a_negative_on_the_row_wins_over_any_positive_beside_it(_facts_db):
    """The writer never leaves a negative beside a positive (a negative clears both paths). The
    read does not rely on that: a row that holds both, however it came to, is not met."""
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await _age("brand-a.example", "negative_at", 0)
    row = await facts.get_fact("brand-a.example")
    assert row["negative_at"] is not None and row["ucp_checkout_at"] is not None
    assert await facts.meets_run_condition("brand-a.example") is False


async def test_a_later_positive_clears_the_negative(_facts_db):
    await facts.record_check("brand-a.example", NEGATIVE)
    assert await facts.meets_run_condition("brand-a.example") is False
    await facts.record_check("brand-a.example", POSITIVE_ACP)
    row = await facts.get_fact("brand-a.example")
    assert row["negative_at"] is None and row["last_outcome"] == "publishes"
    assert await facts.meets_run_condition("brand-a.example") is True


async def test_an_unreadable_check_never_revokes_a_positive(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    before = await facts.get_fact("brand-a.example")
    assert await facts.record_check("brand-a.example", UNREADABLE) is True
    row = await facts.get_fact("brand-a.example")
    assert (row["last_outcome"], row["last_code"]) == ("unreadable", "ucp_timeout")
    assert row["ucp_checkout_at"] == before["ucp_checkout_at"] and row["negative_at"] is None
    assert row["ucp_version"] == "2026-08-25", "an unreadable check keeps the version it had"
    assert await facts.meets_run_condition("brand-a.example") is True


async def test_an_unreadable_check_keeps_an_acp_positive_too(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_ACP)
    before = await facts.get_fact("brand-a.example")
    await facts.record_check("brand-a.example", UNREADABLE)
    assert (await facts.get_fact("brand-a.example"))["acp_checkout_at"] == before["acp_checkout_at"] is not None
    assert await facts.meets_run_condition("brand-a.example") is True


async def test_a_positive_on_one_path_keeps_a_positive_the_other_path_could_not_recheck(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_ACP)
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    row = await facts.get_fact("brand-a.example")
    assert row["acp_checkout_at"] is not None and row["ucp_checkout_at"] is not None


async def test_an_unreadable_check_never_renews_a_positive(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await _age("brand-a.example", "ucp_checkout_at", 31)
    await facts.record_check("brand-a.example", UNREADABLE)
    assert await facts.meets_run_condition("brand-a.example") is False


async def test_an_unreadable_check_never_clears_a_negative(_facts_db):
    await facts.record_check("brand-a.example", NEGATIVE)
    await facts.record_check("brand-a.example", UNREADABLE)
    assert (await facts.get_fact("brand-a.example"))["negative_at"] is not None
    assert await facts.meets_run_condition("brand-a.example") is False


async def test_a_first_check_that_cannot_tell_stores_no_fact(_facts_db):
    assert await facts.record_check("brand-a.example", UNREADABLE) is True
    row = await facts.get_fact("brand-a.example")
    assert (row["ucp_checkout_at"], row["acp_checkout_at"], row["negative_at"]) == (None, None, None)
    assert await facts.meets_run_condition("brand-a.example") is False


@pytest.mark.parametrize("days,met", [(29, True), (31, False)])
async def test_a_positive_counts_for_thirty_days(_facts_db, days, met):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await _age("brand-a.example", "ucp_checkout_at", days)
    assert await facts.meets_run_condition("brand-a.example") is met


async def test_either_fresh_positive_is_enough(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await facts.record_check("brand-a.example", POSITIVE_ACP)
    await _age("brand-a.example", "ucp_checkout_at", 400)
    assert await facts.meets_run_condition("brand-a.example") is True
    await _age("brand-a.example", "acp_checkout_at", 400)
    assert await facts.meets_run_condition("brand-a.example") is False


@pytest.mark.parametrize("raw,days", [("7", 7), ("1", 1), ("90", 90), ("0", 30), ("91", 30), ("-3", 30),
                                      ("7.5", 30), ("week", 30), ("", 30), (" 14 ", 14)])
def test_the_window_is_set_by_env_and_a_bad_value_is_the_default(monkeypatch, raw, days):
    monkeypatch.setenv(facts.MAX_AGE_ENV, raw)
    assert facts.max_age_days() == days


def test_the_window_is_thirty_days_when_unset(monkeypatch):
    monkeypatch.delenv(facts.MAX_AGE_ENV, raising=False)
    assert facts.max_age_days() == facts.DEFAULT_MAX_AGE_DAYS == 30


async def test_the_env_window_is_the_one_the_read_uses(_facts_db, monkeypatch):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await _age("brand-a.example", "ucp_checkout_at", 8)
    assert await facts.meets_run_condition("brand-a.example") is True
    monkeypatch.setenv(facts.MAX_AGE_ENV, "7")
    assert await facts.meets_run_condition("brand-a.example") is False


@pytest.mark.parametrize("check", [
    facts.Check(facts.PUBLISHES, "ok"),                          # says publishes, shows no document that does
    facts.Check(facts.PUBLISHES, "ok", False, None),
    facts.Check(facts.DOES_NOT_PUBLISH, "ok", True, False),      # says no, shows a document that does
    facts.Check(facts.DOES_NOT_PUBLISH, "ok", False, None),      # a negative needs BOTH paths clean
    facts.Check(facts.DOES_NOT_PUBLISH, "ok"),
    facts.Check(facts.UNREADABLE, "ok", False, True),
    facts.Check(facts.UNREADABLE, "ok", False, False),           # both clean is a negative, not "cannot tell"
    facts.Check("maybe", "ok"),
    facts.Check(facts.PUBLISHES, "ok", 1, None),                 # a truthy non-bool is not a True
    facts.Check(facts.UNREADABLE, "ok", 0, None),                # nor a falsy one a False
    ("publishes", "ok", True, None, None),                       # a bare tuple is not a Check
    None,
], ids=["no-evidence", "publishes-on-a-no", "negative-with-evidence", "negative-with-one-path",
        "negative-with-no-path", "unreadable-with-evidence", "unreadable-with-both-clean",
        "unknown-outcome", "truthy-non-bool", "falsy-non-bool", "bare-tuple", "none"])
async def test_a_check_that_contradicts_itself_is_refused(_facts_db, check):
    assert await facts.record_check("brand-a.example", check) is False
    assert await _count() == 0


@pytest.mark.parametrize("source", ["Census", "census run", "", "x" * 33, None, "census;drop"])
async def test_a_source_that_is_not_a_short_token_is_refused(_facts_db, source):
    assert await facts.record_check("brand-a.example", POSITIVE_UCP, source=source) is False
    assert await _count() == 0


async def test_nothing_a_server_sent_is_stored_verbatim(_facts_db):
    hostile = facts.Check(facts.UNREADABLE, "<script>alert(1)</script>", False, None, "2026-08-25'; DROP")
    assert await facts.record_check("brand-a.example", hostile) is True
    row = await facts.get_fact("brand-a.example")
    assert (row["last_code"], row["ucp_version"]) == ("other", None)
    too_long = facts.Check(facts.UNREADABLE, "a" * 65)
    await facts.record_check("brand-b.example", too_long)
    assert (await facts.get_fact("brand-b.example"))["last_code"] == "other"


async def test_a_missing_table_is_no_fact_and_never_an_error(_facts_db):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    await database.execute(f"DROP TABLE {TABLE}")
    assert await facts.table_is_readable() is False
    assert await facts.meets_run_condition("brand-a.example") is False
    assert await facts.get_fact("brand-a.example") is None
    assert await facts.record_check("brand-a.example", POSITIVE_UCP) is False


async def test_an_empty_table_is_readable(_facts_db):
    assert await facts.table_is_readable() is True


async def test_a_slow_read_is_no_fact(_facts_db, monkeypatch):
    await facts.record_check("brand-a.example", POSITIVE_UCP)
    real = database.fetch_one

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        return await real(*args, **kwargs)

    monkeypatch.setattr(facts, "READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(database, "fetch_one", slow)
    assert await facts.meets_run_condition("brand-a.example") is False
    assert await facts.table_is_readable() is False
    assert await facts.get_fact("brand-a.example") is None


async def test_a_failed_write_is_false_and_does_not_raise(_facts_db, monkeypatch):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("connection lost")

    monkeypatch.setattr(database, "fetch_one", broken)
    assert await facts.record_check("brand-a.example", POSITIVE_UCP) is False


# ── the classifier, on rows the job itself built ─────────────────────────────────────────────

UCP_NO_CHECKOUT = copy.deepcopy(UCP_0825)
UCP_NO_CHECKOUT["ucp"]["capabilities"].pop("dev.ucp.shopping.checkout")
ACP_NO_CHECKOUT = copy.deepcopy(ACP_FULL)
ACP_NO_CHECKOUT["capabilities"]["services"] = ["orders", "carts"]


def _body(doc: Any) -> bytes:
    return json.dumps(doc).encode()


def _built(ucp: Any, acp: Any, robots: Optional[str] = census.ROBOTS_ABSENT, why: Optional[str] = None) -> Dict[str, Any]:
    """A row from the job's own `build_row`: each side is (status-or-code, body-or-None)."""
    return census.build_row("brand-a.example", ucp, acp, robots, why)


OK_UCP, OK_ACP = (200, _body(UCP_0825)), (200, _body(ACP_MIN))
GONE = (404, None)


@pytest.mark.parametrize("ucp,acp,outcome,flags", [
    (OK_UCP, GONE, "publishes", (True, False)),
    (GONE, OK_ACP, "publishes", (False, True)),
    (OK_UCP, (200, _body(ACP_FULL)), "publishes", (True, True)),
    (OK_UCP, ("timeout", None), "publishes", (True, None)),           # one readable yes is enough
    ((403, None), OK_ACP, "publishes", (None, True)),
    (GONE, GONE, "does_not_publish", (False, False)),
    ((410, None), GONE, "does_not_publish", (False, False)),
    ((200, _body(UCP_NO_CHECKOUT)), GONE, "does_not_publish", (False, False)),
    ((200, _body(UCP_EMPTY)), (410, None), "does_not_publish", (False, False)),
    (GONE, (200, _body(ACP_NO_CHECKOUT)), "does_not_publish", (False, False)),
], ids=["ucp", "acp", "both", "ucp-yes-acp-timeout", "ucp-403-acp-yes", "both-404", "410-404",
        "ucp-without-checkout", "empty-ucp-and-410", "acp-without-checkout"])
def test_a_clean_answer_is_classified_by_what_the_documents_offer(ucp, acp, outcome, flags):
    check = census.checkout_check(_built(ucp, acp))
    assert (check.outcome, check.code, (check.ucp_checkout, check.acp_checkout)) == (outcome, "ok", flags)
    assert facts._usable(check)


@pytest.mark.parametrize("ucp,acp,code,paths", [
    ((403, None), GONE, "ucp_403", (None, False)),
    ((500, None), GONE, "ucp_500", (None, False)),
    ((429, None), (503, None), "ucp_429", (None, None)),
    (GONE, (500, None), "acp_500", (False, None)),
    (GONE, (301, None), "acp_301", (False, None)),
    (("timeout", None), GONE, "ucp_timeout", (None, False)),
    (GONE, ("redirect_refused", None), "acp_redirect_refused", (False, None)),
    ((200, b"<html>not json</html>"), GONE, "ucp_not_json", (None, False)),
    ((200, _body({"ucp": {"services": {}}})), GONE, "ucp_version_missing", (None, False)),
    ((200, _body(UCP_NO_CHECKOUT)), (200, b"[]"), "acp_not_an_object", (False, None)),
    ((census.CODE_DEADLINE, None), (census.CODE_DEADLINE, None), "deadline", (None, None)),
    ((200, _body(UCP_NO_CHECKOUT)), (census.CODE_DEADLINE, None), "deadline", (False, None)),
], ids=["403", "500", "429-and-503", "acp-500", "acp-301", "timeout", "acp-redirect", "not-json",
        "no-version", "acp-not-object", "deadline", "half-deadline"])
def test_anything_that_is_not_a_clean_answer_is_unreadable(ucp, acp, code, paths):
    """Unreadable overall, with each path's own answer kept: the path that answered cleanly
    without checkout says False (and so loses its stored positive), the other says None."""
    check = census.checkout_check(_built(ucp, acp))
    assert (check.outcome, (check.ucp_checkout, check.acp_checkout)) == ("unreadable", paths)
    assert check.code == code and facts._usable(check)


def test_the_version_of_an_unreadable_check_is_a_readable_profiles_or_none():
    readable_without = census.checkout_check(_built((200, _body(UCP_NO_CHECKOUT)), (403, None)))
    assert (readable_without.outcome, readable_without.ucp_version) == ("unreadable", "2026-08-25")
    assert census.checkout_check(_built(GONE, (403, None))).ucp_version is None
    assert census.checkout_check(_built((403, None), GONE)).ucp_version is None


def test_an_acp_extension_named_checkout_is_not_the_checkout_service():
    """`acp_cap` in a printed row merges services and extensions. The fact reads services only."""
    extension_only = copy.deepcopy(ACP_FULL)
    extension_only["capabilities"]["services"] = ["orders", "carts"]
    extension_only["capabilities"]["extensions"] = [{"name": "checkout"}, "checkout"]
    row = _built(GONE, (200, _body(extension_only)))
    assert "checkout" in row["acp_cap"], "the merged list does name it"
    check = census.checkout_check(row)
    assert (check.outcome, check.acp_checkout) == ("does_not_publish", False)
    assert census.checkout_check(_built(GONE, (200, _body(ACP_FULL)))).acp_checkout is True


def test_the_services_key_is_never_printed():
    row = _built(OK_UCP, (200, _body(ACP_FULL)))
    assert row[census._ACP_SERVICES] == ["checkout", "orders", "delegate_payment", "carts"]
    printed = census.printable_row(row)
    assert not [key for key in printed if key.startswith("_")]
    assert set(printed) == set(census.empty_row("brand-a.example"))


def test_a_403_on_one_path_is_never_read_as_publishes_nothing():
    """The mirror of the mistake the store guards: a bot wall on the UCP path with a clean 404 on
    ACP is "cannot tell", so it must not revoke a merchant's positive."""
    assert census.checkout_check(_built((403, None), GONE)).outcome == "unreadable"
    assert census.checkout_check(_built(GONE, GONE)).outcome == "does_not_publish"


@pytest.mark.parametrize("state,why", [(census.ROBOTS_UNAVAILABLE, "503"), (census.ROBOTS_DISALLOWED, None)])
def test_a_path_robots_kept_us_from_is_unreadable(state, why):
    refusal = census.CODE_ROBOTS_UNAVAILABLE if state == census.ROBOTS_UNAVAILABLE else census.CODE_ROBOTS_DISALLOWED
    check = census.checkout_check(_built((refusal, None), GONE, state, why))
    assert check.outcome == "unreadable" and re.fullmatch(r"[a-z0-9_]{1,64}", check.code)


def test_the_version_stored_is_the_readable_profiles_own():
    assert census.checkout_check(_built(OK_UCP, GONE)).ucp_version == "2026-08-25"
    assert census.checkout_check(_built((200, _body(UCP_EMPTY)), GONE)).ucp_version == "2026-01-23"
    assert census.checkout_check(_built(GONE, OK_ACP)).ucp_version is None


# ── the job, end to end, with --store ────────────────────────────────────────────────────────


def _args(domains: str, store: bool = True) -> argparse.Namespace:
    return argparse.Namespace(domains=domains, domains_file="", from_directory=False,
                              deadline_seconds=census.DEFAULT_DEADLINE_SECONDS, store=store)


async def _amain(net: Net, domains: str, store: bool = True) -> Run:
    lines: List[str] = []
    code = await census.amain(_args(domains, store), out=lines.append, **net.seams())
    return Run(code, lines)


def _store_line(run: Run) -> Optional[Dict[str, int]]:
    found = [json.loads(x[len(census.STORE_PREFIX):]) for x in run.lines if x.startswith(census.STORE_PREFIX)]
    assert len(found) <= 1
    return found[0] if found else None


ROUTES = {
    ("shop-a.example", U): _json(UCP_0825),
    ("shop-b.example", A): _json(ACP_MIN),
    ("shop-d.example", U): _raw(403),
}  # shop-c answers 404 on everything


async def test_a_storing_run_writes_one_fact_per_domain(_facts_db):
    net = Net(ROUTES)
    got = await _amain(net, "shop-a.example,shop-b.example,shop-c.example,shop-d.example")
    assert got.code == census.EXIT_OK and got.lines[-1] == census.DONE_LINE
    assert _store_line(got) == {"written": 4, "failed": 0, "skipped": 0,
                                "publishes": 2, "does_not_publish": 1, "unreadable": 1}
    assert [await facts.meets_run_condition(f"shop-{x}.example") for x in "abcd"] == [True, True, False, False]
    assert (await facts.get_fact("shop-a.example"))["ucp_version"] == "2026-08-25"
    assert (await facts.get_fact("shop-c.example"))["negative_at"] is not None
    assert (await facts.get_fact("shop-d.example"))["last_code"] == "ucp_403"
    assert database.is_connected, "the run leaves a connection it did not open as it found it"


async def test_a_second_run_that_cannot_read_a_merchant_keeps_its_fact(_facts_db):
    await _amain(Net(ROUTES), "shop-a.example")
    blocked = Net({("shop-a.example", R): _raw(503)})
    got = await _amain(blocked, "shop-a.example")
    assert _store_line(got)["unreadable"] == 1
    assert await facts.meets_run_condition("shop-a.example") is True


async def test_a_second_run_that_reads_the_profile_without_checkout_ends_the_fact(_facts_db):
    """Review case A, end to end: the profile stops listing checkout while ACP answers 403."""
    await _amain(Net(ROUTES), "shop-a.example")
    later = Net({("shop-a.example", U): _json(UCP_NO_CHECKOUT), ("shop-a.example", A): _raw(403)})
    got = await _amain(later, "shop-a.example")
    assert _store_line(got)["unreadable"] == 1
    row = await facts.get_fact("shop-a.example")
    assert (row["ucp_checkout_at"], row["negative_at"], row["last_code"]) == (None, None, "acp_403")
    assert await facts.meets_run_condition("shop-a.example") is False


async def test_a_second_run_that_finds_the_profile_gone_revokes_it(_facts_db):
    await _amain(Net(ROUTES), "shop-a.example")
    got = await _amain(Net({}), "shop-a.example")
    assert _store_line(got)["does_not_publish"] == 1
    assert await facts.meets_run_condition("shop-a.example") is False


async def test_the_fact_is_taken_from_the_uncut_row(_facts_db):
    """A profile with more capabilities than a printed row keeps: the printed row loses the
    checkout capability, the stored fact must not."""
    wide = copy.deepcopy(UCP_0825)
    checkout = wide["ucp"]["capabilities"].pop("dev.ucp.shopping.checkout")
    wide["ucp"]["capabilities"] = {
        **{f"dev.example.capability{i:02d}": checkout for i in range(census.MAX_ROW_LIST + 5)},
        "dev.ucp.shopping.checkout": checkout,
    }
    got = await _amain(Net({("shop-a.example", U): _json(wide)}), "shop-a.example")
    printed = got.row("shop-a.example")
    assert printed["truncated"] is True and "dev.ucp.shopping.checkout" not in printed["cap"]
    assert await facts.meets_run_condition("shop-a.example") is True


async def test_storing_adds_exactly_one_line_to_what_the_job_prints(_facts_db):
    domains = "shop-a.example,shop-c.example"
    plain = await _amain(Net(ROUTES), domains, store=False)
    stored = await _amain(Net(ROUTES), domains, store=True)
    assert _store_line(plain) is None
    extra = [x for x in stored.lines if x not in plain.lines]
    assert len(extra) == 1 and extra[0].startswith(census.STORE_PREFIX)
    assert [x for x in stored.lines if x not in extra] == plain.lines
    assert stored.lines.index(extra[0]) == len(stored.lines) - 2, "after the totals, before the done line"


async def test_a_run_without_store_writes_nothing(_facts_db):
    got = await _amain(Net(ROUTES), "shop-a.example", store=False)
    assert got.code == census.EXIT_OK and await _count() == 0


async def test_a_storing_run_refuses_before_its_first_request_when_the_table_is_missing(_facts_db):
    await database.execute(f"DROP TABLE {TABLE}")
    net = Net(ROUTES)
    got = await _amain(net, "shop-a.example,shop-b.example")
    assert got.code == census.EXIT_REFUSED
    assert got.lines == [f"UCPPROF_REFUSED store_unavailable table={TABLE}; nothing was read"]
    assert net.requests == [] and net.resolved == []


async def test_a_storing_run_refuses_when_the_database_cannot_be_reached(monkeypatch):
    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("no database")

    # Both doors: on Postgres the bounded connect builds its own pool and never calls
    # `database.connect`, so patching that alone leaves a database that really connects.
    monkeypatch.setattr(database, "is_connected", False)
    monkeypatch.setattr(database, "connect", refuse)
    monkeypatch.setattr(database_readiness, "connect_database_with_timeout", refuse)
    net = Net(ROUTES)
    got = await _amain(net, "shop-a.example")
    assert got.code == census.EXIT_REFUSED and net.requests == []
    assert got.lines == ["UCPPROF_REFUSED store_unavailable error_type=OSError; nothing was read"]


@pytest.mark.parametrize("failure", ["false", "none", "raises"])
async def test_a_write_that_fails_is_counted_and_the_run_goes_on(_facts_db, monkeypatch, failure):
    real = facts.record_check

    async def flaky(domain: Any, check: Any, **kwargs: Any) -> Any:
        if domain == "shop-a.example":
            if failure == "raises":
                raise OSError("connection lost")
            return False if failure == "false" else None
        return await real(domain, check, **kwargs)

    monkeypatch.setattr(facts, "record_check", flaky)
    got = await _amain(Net(ROUTES), "shop-a.example,shop-b.example")
    assert got.code == census.EXIT_STORE_FAILED and got.lines[-1] == census.DONE_LINE
    assert _store_line(got) == {"written": 1, "failed": 1, "skipped": 0,
                                "publishes": 1, "does_not_publish": 0, "unreadable": 0}
    assert await facts.meets_run_condition("shop-b.example") is True
    assert await facts.get_fact("shop-a.example") is None


async def test_a_row_with_no_usable_domain_is_skipped_not_failed(_facts_db):
    got = await _amain(Net(ROUTES), "shop-a.example,not a domain")
    assert got.code == census.EXIT_OK
    assert _store_line(got) == {"written": 1, "failed": 0, "skipped": 1,
                                "publishes": 1, "does_not_publish": 0, "unreadable": 0}
    assert await _count() == 1


def _on_a_fresh_loop(coro_factory: Any) -> Any:
    async def scoped() -> Any:
        await database.connect()
        try:
            return await coro_factory()
        finally:
            await database.disconnect()

    return asyncio.run(scoped())


def test_the_command_line_stores_and_closes_what_it_opened(monkeypatch):
    """Through `main`, as the one-off runs it: the job connects, stores and disconnects itself."""
    if database.is_connected:
        pytest.skip("another test left the shared database object connected")
    _assert_throwaway_database()

    async def prepare() -> None:
        await _create_table()
        await database.execute(f"DELETE FROM {TABLE}")

    async def read_and_clean() -> Any:
        met = await facts.meets_run_condition("shop-a.example")
        await database.execute(f"DELETE FROM {TABLE}")
        return met

    _on_a_fresh_loop(prepare)
    monkeypatch.setenv(census.FLAG, "on")
    net = Net(ROUTES)
    lines: List[str] = []
    code = census.main(["--domains", "shop-a.example", "--store"], out=lines.append, **net.seams())
    try:
        assert code == census.EXIT_OK and any(x.startswith(census.STORE_PREFIX) for x in lines)
        assert database.is_connected is False
    finally:
        met = _on_a_fresh_loop(read_and_clean)
    assert met is True


def test_the_dial_off_wins_over_store(monkeypatch):
    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the job touched the database with its dial off")

    monkeypatch.delenv(census.FLAG, raising=False)
    monkeypatch.setattr(database, "connect", never)
    monkeypatch.setattr(database, "fetch_one", never)
    net = Net(ROUTES)
    lines: List[str] = []
    assert census.main(["--domains", "shop-a.example", "--store"], out=lines.append, **net.seams()) == census.EXIT_OK
    assert lines == [census.DISABLED_LINE] and net.requests == []


# ── the table has one creation path ──────────────────────────────────────────────────────────


def _columns(ddl: str) -> List[str]:
    inside = ddl[ddl.index("(") + 1:ddl.rindex(")")]
    names = re.findall(r"^\s{4}([a-z_]+) [A-Z]", inside, flags=re.M)
    return names


def test_the_sqlite_table_has_the_migrations_columns_and_check():
    migration = "\n".join(_migration_statements())
    assert _columns(migration) == _columns(_CREATE_SQL_SQLITE) == [
        "merchant_domain", "checked_at", "last_outcome", "last_code",
        "ucp_checkout_at", "acp_checkout_at", "negative_at", "ucp_version", "source"]
    check = "CHECK (last_outcome IN ('publishes', 'does_not_publish', 'unreadable'))"
    assert check in migration and check in _CREATE_SQL_SQLITE
    assert {f"'{o}'" for o in facts.OUTCOMES} == set(re.findall(r"'[a-z_]+'", check))


def test_nothing_at_startup_creates_the_table():
    """Migration 294 is applied by hand, once, as a gated one-off. A startup heal would build the
    table on the next deploy, ahead of that run."""
    for path in ("db/schema_guard.py", "main.py", "db/sql_migrations.py"):
        assert TABLE not in (_REPO / path).read_text("utf-8"), path
    creators = sorted(
        str(p.relative_to(_REPO)) for p in _REPO.glob("**/*.py")
        if ".venv" not in p.parts and "node_modules" not in p.parts and p.relative_to(_REPO).parts[0] != "tests"
        and f"CREATE TABLE IF NOT EXISTS {TABLE}" in p.read_text("utf-8", errors="ignore"))
    assert creators == [], "only migration 294 (and this suite's SQLite copy) may create the table"


def test_the_migration_is_one_plain_create_and_its_down_one_drop():
    statements = _migration_statements()
    assert len(statements) == 1 and statements[0].startswith(f"CREATE TABLE IF NOT EXISTS {TABLE} (")
    text = _MIGRATION.read_text("utf-8").upper()
    assert "CONCURRENTLY" not in text and "DROP " not in text and "ALTER " not in text
    down = [line for line in _DOWN.read_text("utf-8").splitlines() if line and not line.startswith("--")]
    assert down == [f"DROP TABLE IF EXISTS {TABLE};"]


def test_nothing_reads_the_fact_on_a_request_path_yet():
    """This change stores the fact. The filter that reads it is its own change, behind a flag."""
    for folder in ("routes", "services"):
        for path in (_REPO / folder).rglob("*.py"):
            assert "merchant_agent_checkout_facts" not in path.read_text("utf-8", errors="ignore"), path
