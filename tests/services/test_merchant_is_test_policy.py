"""The staff-set is_test leg of the rig exclusion (migration 282, MERCHANT_IS_TEST_FILTER_ENABLED).

Flag off: the column is never read and every answer is what it was before it existed.
Flag on: is_test merchants join the exclusion set; a failed is_test read keeps its last good set
and never costs the demo-domain leg.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services import test_merchant_policy as pol

ON = {pol.IS_TEST_FILTER_ENV: "true"}


@pytest.fixture(autouse=True)
def _clear_cache():
    pol.reset_cache()
    yield
    pol.reset_cache()


class _RoutingDB:
    """fetch_all answers by which table the query reads; records every query."""

    def __init__(
        self,
        demo: List[Dict[str, Any]] | None = None,
        is_test: List[Dict[str, Any]] | None = None,
        demo_raises: bool = False,
        is_test_raises: bool = False,
    ):
        self.demo = demo or []
        self.is_test = is_test or []
        self.demo_raises = demo_raises
        self.is_test_raises = is_test_raises
        self.queries: List[str] = []

    def is_test_reads(self) -> int:
        return sum(1 for q in self.queries if "is_test" in q)

    async def fetch_all(self, query, values=None):
        self.queries.append(query)
        if "is_test" in query:
            if self.is_test_raises:
                raise RuntimeError("column merchant_onboarding.is_test does not exist")
            return self.is_test
        if self.demo_raises:
            raise RuntimeError("db down")
        return self.demo


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_flag_truthy_values(raw):
    assert pol.merchant_is_test_filter_enabled({pol.IS_TEST_FILTER_ENV: raw}) is True


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", "enabled"])
def test_flag_is_off_unless_truthy(raw):
    assert pol.merchant_is_test_filter_enabled({pol.IS_TEST_FILTER_ENV: raw}) is False


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(pol.IS_TEST_FILTER_ENV, raising=False)
    assert pol.merchant_is_test_filter_enabled() is False
    assert pol.merchant_is_test_filter_enabled({}) is False


async def test_flag_off_never_reads_the_column_and_answers_as_before():
    db = _RoutingDB(demo=[{"merchant_id": "m_demo"}], is_test=[{"merchant_id": "m_staff_test"}])
    ids = await pol.get_excluded_merchant_ids(db, env={})
    assert db.is_test_reads() == 0
    assert ids == pol.static_test_merchant_ids({}) | {"m_demo"}
    assert "m_staff_test" not in ids


async def test_flag_on_adds_is_test_merchants():
    db = _RoutingDB(demo=[{"merchant_id": "m_demo"}], is_test=[{"merchant_id": "m_staff_test"}])
    ids = await pol.get_excluded_merchant_ids(db, env=ON)
    assert {"m_demo", "m_staff_test"} <= ids
    assert pol.static_test_merchant_ids({}) <= ids


async def test_flag_on_result_is_cached_with_the_demo_set():
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.get_excluded_merchant_ids(db, env=ON, now=1000.0)
    ids = await pol.get_excluded_merchant_ids(db, env=ON, now=1100.0)
    assert len(db.queries) == 2  # one demo read + one is_test read, then a cache hit
    assert "m_staff_test" in ids


async def test_a_flag_off_cache_is_not_a_hit_for_a_flag_on_call():
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.get_excluded_merchant_ids(db, env={}, now=1000.0)
    ids = await pol.get_excluded_merchant_ids(db, env=ON, now=1001.0)
    assert db.is_test_reads() == 1
    assert "m_staff_test" in ids


async def test_flag_off_after_on_drops_the_is_test_ids_at_once():
    db = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.get_excluded_merchant_ids(db, env=ON, now=1000.0)
    ids = await pol.get_excluded_merchant_ids(db, env={}, now=1001.0)
    assert "m_staff_test" not in ids


async def test_a_failed_is_test_read_keeps_the_demo_leg():
    db = _RoutingDB(demo=[{"merchant_id": "m_demo"}], is_test_raises=True)
    ids = await pol.get_excluded_merchant_ids(db, env=ON)
    assert "m_demo" in ids
    assert pol.static_test_merchant_ids({}) <= ids


async def test_a_failed_is_test_read_serves_the_last_good_set(caplog):
    good = _RoutingDB(is_test=[{"merchant_id": "m_staff_test"}])
    await pol.get_excluded_merchant_ids(good, env=ON, now=0.0)
    bad = _RoutingDB(is_test_raises=True)
    ids = await pol.get_excluded_merchant_ids(bad, env=ON, now=1000.0)  # cache expired
    assert bad.is_test_reads() == 1
    assert "m_staff_test" in ids
    assert any("is_test resolve FAILED" in r.getMessage() for r in caplog.records)


async def test_unknown_is_not_test_when_the_column_was_never_readable():
    # The fail direction: a merchant the read could not see lists as before; static ids still apply.
    db = _RoutingDB(is_test_raises=True)
    ids = await pol.get_excluded_merchant_ids(db, env=ON)
    assert ids == pol.static_test_merchant_ids({})


async def test_is_test_warning_is_rate_limited(caplog):
    bad = _RoutingDB(is_test_raises=True)
    await pol.get_excluded_merchant_ids(bad, env=ON, now=0.0)
    pol.invalidate_cache()
    await pol.get_excluded_merchant_ids(bad, env=ON, now=10.0)
    assert bad.is_test_reads() == 2
    warnings = [r for r in caplog.records if "is_test resolve FAILED" in r.getMessage()]
    assert len(warnings) == 1


async def test_invalidate_forces_a_reread_and_keeps_the_last_good_sets():
    db = _RoutingDB(demo=[{"merchant_id": "m_demo"}], is_test=[{"merchant_id": "m_a"}])
    await pol.get_excluded_merchant_ids(db, env=ON, now=1000.0)
    db.is_test = [{"merchant_id": "m_b"}]
    pol.invalidate_cache()
    ids = await pol.get_excluded_merchant_ids(db, env=ON, now=1001.0)  # inside the TTL
    assert "m_b" in ids and "m_a" not in ids
    # A failed re-read after an invalidate still serves the last good sets.
    pol.invalidate_cache()
    db.demo_raises = True
    ids = await pol.get_excluded_merchant_ids(db, env=ON, now=1002.0)
    assert {"m_demo", "m_b"} <= ids


async def test_blank_ids_from_the_column_are_ignored():
    db = _RoutingDB(is_test=[{"merchant_id": " "}, {"merchant_id": None}, {"merchant_id": "m_x "}])
    ids = await pol.get_excluded_merchant_ids(db, env=ON)
    assert "m_x" in ids and "" not in ids and " " not in ids
