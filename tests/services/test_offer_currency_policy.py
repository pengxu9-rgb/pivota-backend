"""Currency-defect exclusion on the connected/external-seed lane.

On 2026-07-28 the public UNAUTHENTICATED ACP feed served seven Mintree rows
priced 847-3927.70 and labelled "USD". Mintree is an Indian store: those were
rupee prices published as US dollars. The suppression that blocks them on the
index feed, the sitemap and the PDP renderer lives on `catalog_offers`, and the
`find_products` lane reads none of it.

Two properties this file exists to hold:

1. The gate READS the shared source (`catalog_offers.suppression_reason`)
   rather than restating the rule as a hand-kept denylist.
2. The gate is actually WIRED into the served slate. That second one is the
   whole ballgame: on PR #1631, a mutant that deleted the argument connecting
   two correct halves left 23 isolation tests green. The module being right
   proves nothing about the lane being gated, so `test_wiring_*` below drives
   the real choke-point wrapper.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services.offer_currency_policy import (
    CURRENCY_DEFECT_SUPPRESSION_REASON,
    domain_matches,
    filter_out_currency_defect_rows,
    get_currency_defect_domains,
    is_currency_defect_row,
    normalize_domain,
    product_domains,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


class _FakeDB:
    """Records the SQL so we can assert WHICH source the gate reads."""

    def __init__(self, rows: Any = None, raises: bool = False):
        self._rows = rows if rows is not None else []
        self._raises = raises
        self.queries: List[str] = []
        self.values: List[Dict[str, Any]] = []

    async def fetch_all(self, query, values=None):
        self.queries.append(str(query))
        self.values.append(dict(values or {}))
        if self._raises:
            raise RuntimeError("db down")
        return self._rows


# ---------------------------------------------------------------------------
# domain normalisation -- both sides of the comparison come from different places
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mintree.us", "mintree.us"),
        ("MINTREE.US", "mintree.us"),
        ("www.mintree.us", "mintree.us"),
        ("https://www.mintree.us/products/foo?x=1", "mintree.us"),
        ("http://mintree.us:8443/a/b", "mintree.us"),
        ("mintree.us/products/foo", "mintree.us"),
        ("  mintree.us.  ", "mintree.us"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_subdomains_match_but_lookalikes_do_not():
    """Label-boundary matching. Over-matching silently deletes real products."""
    defects = {"mintree.us"}
    assert domain_matches("mintree.us", defects) is True
    assert domain_matches("shop.mintree.us", defects) is True
    assert domain_matches("notmintree.us", defects) is False
    assert domain_matches("mintree.us.evil.com", defects) is False
    assert domain_matches("us", defects) is False


# ---------------------------------------------------------------------------
# the shared source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolver_reads_catalog_offers_suppression_not_a_denylist():
    db = _FakeDB([{"domain": "mintree.us"}, {"domain": "reddane.co.za"}])
    domains = await get_currency_defect_domains(db)
    assert domains == {"mintree.us", "reddane.co.za"}

    sql = db.queries[0].lower()
    assert "catalog_offers" in sql, "the gate must read the shared source"
    assert "suppression_reason" in sql and "suppressed_at" in sql
    assert db.values[0]["reason"] == CURRENCY_DEFECT_SUPPRESSION_REASON


@pytest.mark.asyncio
async def test_resolver_normalizes_what_the_db_returns():
    db = _FakeDB([{"domain": "  WWW.Mintree.US  "}])
    assert await get_currency_defect_domains(db) == {"mintree.us"}


@pytest.mark.asyncio
async def test_resolver_is_fail_soft_on_db_error():
    """A resolver hiccup must not crash search."""
    assert await get_currency_defect_domains(_FakeDB(raises=True)) == set()


@pytest.mark.asyncio
async def test_resolver_caches_within_ttl():
    db = _FakeDB([{"domain": "mintree.us"}])
    assert await get_currency_defect_domains(db, now=1000.0) == {"mintree.us"}
    assert await get_currency_defect_domains(db, now=1100.0) == {"mintree.us"}
    assert len(db.queries) == 1, "cached within the TTL window"
    await get_currency_defect_domains(db, now=1000.0 + 301.0)
    assert len(db.queries) == 2, "re-resolved after the TTL"


@pytest.mark.asyncio
async def test_none_db_returns_empty():
    assert await get_currency_defect_domains(None) == set()


# ---------------------------------------------------------------------------
# row attribution
# ---------------------------------------------------------------------------

def _mintree_row(**over: Any) -> Dict[str, Any]:
    row = {
        "id": "ext_fb756aa379b28bd89a",
        "merchant_id": None,  # <- why the sibling rig filter cannot see this row
        "brand": "Mintree",
        "price": 1058,
        "currency": "USD",  # <- the defect: this is rupees
        "external_destination_url": "https://mintree.us/products/kumkumadi-oil",
    }
    row.update(over)
    return row


def test_external_seed_rows_are_attributed_by_destination_url():
    assert "mintree.us" in product_domains(_mintree_row())


def test_the_live_defect_row_is_dropped():
    """The exact shape observed on prod 2026-07-28."""
    assert is_currency_defect_row(_mintree_row(), {"mintree.us"}) is True


def test_a_real_usd_store_is_kept():
    row = _mintree_row(
        brand="Beplain", external_destination_url="https://beplain.com/products/x"
    )
    assert is_currency_defect_row(row, {"mintree.us"}) is False


def test_rows_with_no_resolvable_domain_are_kept():
    """Fail OPEN on unknown provenance.

    This gate stops a known mislabelled storefront from publishing; requiring
    every row to prove its provenance would empty the lane instead.
    """
    assert is_currency_defect_row({"id": "x", "brand": "Y"}, {"mintree.us"}) is False


def test_object_rows_work_not_just_dicts():
    class _P:
        external_destination_url = "https://shop.mintree.us/p/1"

    assert is_currency_defect_row(_P(), {"mintree.us"}) is True


def test_filter_drops_only_the_defect_rows():
    rows = [
        _mintree_row(),
        _mintree_row(external_destination_url="https://beplain.com/p/1", brand="Beplain"),
        _mintree_row(external_destination_url="https://shop.mintree.us/p/2"),
        {"id": "no-domain"},
    ]
    kept = filter_out_currency_defect_rows(rows, {"mintree.us"})
    assert [r.get("brand") or r.get("id") for r in kept] == ["Beplain", "no-domain"]


def test_empty_defect_set_is_a_no_op():
    rows = [_mintree_row()]
    assert filter_out_currency_defect_rows(rows, set()) == rows


def test_filter_handles_empty_input():
    assert filter_out_currency_defect_rows(None, {"mintree.us"}) == []
    assert filter_out_currency_defect_rows([], {"mintree.us"}) == []


# ---------------------------------------------------------------------------
# THE WIRING -- the only tests that prove the LANE is gated
# ---------------------------------------------------------------------------
#
# Everything above tests the module in isolation, which is exactly the state
# that let a disconnect mutant survive 23 green tests on PR #1631. A correct
# gate that nothing calls is indistinguishable from no gate. These drive the
# real choke-point wrapper (_handle_find_products_multi), stubbing only the
# inner slate producer, and assert on what the wrapper RETURNS.
#
# The sibling rig filter has no equivalent test. That is not a reason to skip
# this one; if anything it is why this leak reached production.

@pytest.fixture
def _wrapper(monkeypatch):
    """The real wrapper, with the inner slate stubbed and the DB faked."""
    import routes.agent_shop_gateway as gw

    def _install(products: List[Dict[str, Any]], defect_rows: Any = None):
        async def _fake_inner(payload, request_metadata, background_tasks):
            return {
                "products": list(products),
                "total": len(products),
                "page": 1,
                "page_size": len(products),
            }

        monkeypatch.setattr(gw, "_handle_find_products_multi_inner", _fake_inner)
        # Neutralise the neighbouring side-effects so this test observes the
        # currency gate and nothing else. All three are already wrapped in
        # try/except in the wrapper, so this only removes noise.
        monkeypatch.setattr(gw, "_record_gateway_decision_events", lambda *a, **k: None)

        async def _noop_redirects(products, tool=None):
            return None

        monkeypatch.setattr(gw, "_attach_connected_product_redirects", _noop_redirects)

        import db.database as dbmod

        monkeypatch.setattr(
            dbmod, "database", _FakeDB(defect_rows if defect_rows is not None else [])
        )
        return gw

    return _install


def _run(gw, monkeypatch):
    import asyncio

    from fastapi import BackgroundTasks

    payload = gw.FindProductsMultiPayload.model_validate({"search": {"query": "serum"}})
    return asyncio.run(
        gw._handle_find_products_multi(payload, None, BackgroundTasks(), emit_decision_event=False)
    )


def test_wiring_the_lane_drops_the_mintree_row(_wrapper, monkeypatch):
    """The exact live defect: a rupee price labelled USD, publicly served."""
    gw = _wrapper(
        [_mintree_row(), _mintree_row(external_destination_url="https://beplain.com/p/1", brand="Beplain")],
        defect_rows=[{"domain": "mintree.us"}],
    )
    result = _run(gw, monkeypatch)
    brands = [p.get("brand") for p in result["products"]]
    assert brands == ["Beplain"], (
        f"the currency gate is not wired into the served slate: {brands}"
    )


def test_wiring_keeps_counters_honest(_wrapper, monkeypatch):
    """A shrunken slate must not read as a ranking regression."""
    gw = _wrapper(
        [_mintree_row(), _mintree_row(external_destination_url="https://beplain.com/p/1")],
        defect_rows=[{"domain": "mintree.us"}],
    )
    result = _run(gw, monkeypatch)
    assert result["total"] == 1
    assert result["page_size"] == 1


def test_wiring_is_a_no_op_when_nothing_is_suppressed(_wrapper, monkeypatch):
    """No suppressed offers -> the lane is untouched, byte for byte."""
    rows = [_mintree_row(), _mintree_row(external_destination_url="https://beplain.com/p/1")]
    gw = _wrapper(rows, defect_rows=[])
    result = _run(gw, monkeypatch)
    assert len(result["products"]) == 2
    assert result["total"] == 2


def test_wiring_serves_the_slate_when_the_gate_itself_fails(_wrapper, monkeypatch, caplog):
    """Fail OPEN on a DB error -- but never SILENTLY.

    Failing open means mislabelled prices are publishable again. A bare `pass`
    would recreate the original bug with nothing to notice it by, so the
    warning is part of the contract, not decoration.
    """
    import logging

    import routes.agent_shop_gateway as gw_mod

    gw = _wrapper([_mintree_row()], defect_rows=[{"domain": "mintree.us"}])

    async def _explode(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(
        gw_mod, "_handle_find_products_multi_inner", gw_mod._handle_find_products_multi_inner
    )
    import services.offer_currency_policy as pol

    monkeypatch.setattr(pol, "get_currency_defect_domains", _explode)

    with caplog.at_level(logging.WARNING):
        result = _run(gw, monkeypatch)

    assert len(result["products"]) == 1, "a gate failure must not empty the lane"
    assert any(
        "currency_defect_filter_failed" in r.message for r in caplog.records
    ), "the gate failed open with no warning — the leak would be invisible again"
