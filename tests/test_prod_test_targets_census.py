"""The targets census is read-only by construction: one READ ONLY repeatable-read transaction, asserted, with its
timeouts, rolled back whatever happens; it selects no store key; and it prints only marker lines."""

from __future__ import annotations

import asyncio
import json

import pytest

from scripts.ops import prod_test_targets_census as census


class FakeConn:
    def __init__(self, *, read_only="on", fail_on=None):
        self.read_only, self.fail_on = read_only, fail_on
        self.executed, self.fetched = [], []
        self.tx = None

    def transaction(self, **kwargs):
        conn = self

        class Tx:
            def __init__(self):
                self.kwargs, self.started, self.rolled_back, self.committed = kwargs, False, False, False

            async def start(self):
                self.started = True

            async def rollback(self):
                self.rolled_back = True

            async def commit(self):  # pragma: no cover - must never be called
                self.committed = True

        self.tx = Tx()
        return self.tx

    async def fetchval(self, query):
        return self.read_only

    async def execute(self, query):
        self.executed.append(query)

    async def fetchrow(self, query, *args):
        self.fetched.append((query, args))
        return {"rows": 2, "merchants": 1}

    async def fetch(self, query, *args):
        self.fetched.append((query, args))
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("statement timeout")
        if "merchant_onboarding" in query:
            return [{"merchant_id": "merch_onboarded_test"}]
        if "FROM products_cache pc\nWHERE" in query:
            return [{"merchant_id": "m", "platform": "shopify", "platform_product_id": "1", "currency": "USD",
                     "expires_at": None, "sgd_seed_sources": 0, "sgd_cache_sources": 0}]
        return []


def run(conn):
    return asyncio.run(census.census(conn, known=["merch_known_test"], quarantine="AND TRUE"))


def test_one_read_only_repeatable_read_transaction_with_its_timeouts_rolled_back():
    conn = FakeConn()
    lines = run(conn)
    assert conn.tx.kwargs == {"readonly": True, "isolation": "repeatable_read"}
    assert conn.tx.started and conn.tx.rolled_back and not conn.tx.committed
    assert conn.executed == ["SET LOCAL statement_timeout = '90s'", "SET LOCAL lock_timeout = '5s'",
                             "SET LOCAL idle_in_transaction_session_timeout = '300s'"]
    assert lines[0] == {"kind": "test_merchants", "count": 2, "ids": ["merch_known_test", "merch_onboarded_test"]}
    assert [line["kind"] for line in lines] == ["test_merchants", "offers_scope", "offers"]


def test_it_refuses_a_session_that_is_not_read_only_and_still_rolls_back():
    conn = FakeConn(read_only="off")
    with pytest.raises(AssertionError, match="READ ONLY"):
        run(conn)
    assert conn.tx.rolled_back and conn.fetched == []


def test_a_failing_statement_still_rolls_back():
    conn = FakeConn(fail_on="catalog_products cp")
    with pytest.raises(RuntimeError):
        run(conn)
    assert conn.tx.rolled_back


def test_every_query_is_a_select_bound_to_the_test_merchants_and_the_quarantine_is_applied():
    conn = FakeConn()
    run(conn)
    queries = [q for q, _ in conn.fetched]
    for query in queries:
        head = query.strip().split(None, 1)[0].upper()
        assert head in ("SELECT", "WITH")
        for word in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "COPY"):
            assert f" {word} " not in f" {query.upper()} "
        assert "{quarantine}" not in query and "{{" not in query
    bound = [args for q, args in conn.fetched if "$1" in q]
    assert bound and all(args == (["merch_known_test", "merch_onboarded_test"],) for args in bound)
    assert sum("AND TRUE" in q for q in queries) == 2        # offers' SGD seeds and the storefront seeds


def test_the_store_key_is_touched_only_by_null_and_length_never_its_content():
    # Never read a secret value, even partly: no selection, no LIKE / ILIKE / ~ / substring / position on the key.
    import re

    expressions = re.findall(r"[^\n]*ms\.api_key[^\n]*", census.KERNEL_SQL)
    assert expressions == ["           (ms.api_key IS NOT NULL AND length(ms.api_key) > 0) AS store_key_present"]
    for other in (census.OFFERS_SQL, census.STOREFRONT_SQL, census.UNEXPIRED_TEST_ROWS_SQL):
        assert "api_key" not in other
    assert "store_key_present" in census.KERNEL_SQL and "names_storefront" not in census.KERNEL_SQL


def test_output_is_marker_lines_only_and_the_file_has_no_at_sign():
    lines = census.render([{"kind": "x", "n": 1}])
    assert lines == ["PTT_TARGETS " + json.dumps({"kind": "x", "n": 1}, sort_keys=True)]
    assert "@" not in open(census.__file__).read()
    assert census.LIMIT == 3
