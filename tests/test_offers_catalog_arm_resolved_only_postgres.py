"""The catalog arm's resolved-only statement (OFFERS_CATALOG_ARM_RESOLVED_ONLY) on POSTGRES.

tests/test_offers_catalog_arm_resolved_only.py proves the behaviour with the reads faked by SQL shape.
This file sends the resolved-only statement through the REAL handler to a real Postgres: its new
`d.product_key = ANY(:resolved_keys)` arm with a list bind, an empty one included. The group-sibling Postgres file's scratch schema and rows are reused (three listings on one
content_key, resolved by applied same-size moves, or not).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.test_offers_resolve_group_siblings_postgres import (  # noqa: F401  (schema_engine: the fixture)
    CK,
    RETAILER_PK,
    RETAILER_SIG,
    _resolve,
    _rows,
    _sources,
    pytestmark,
    schema_engine,
)

ARM_FLAG = "OFFERS_CATALOG_ARM_RESOLVED_ONLY"


def _catalog_ids(res):
    return sorted(o["offer_id"] for o in res["offers"] if (o.get("source") or {}).get("type") == "catalog_offer")


def _arm(monkeypatch, ident, mode):
    monkeypatch.setenv(ARM_FLAG, mode)
    return _resolve(monkeypatch, ident, flag="0")  # the group lane off: the arm reads the resolution itself


def test_a_resolved_group_serves_its_resolved_listings_on_postgres(schema_engine, monkeypatch):  # noqa: F811
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc) - timedelta(days=1))
    for ident in (RETAILER_SIG, CK):
        res = _arm(monkeypatch, ident, "on")
        assert _catalog_ids(res) == ["of:catalog_offer:of_pg_sk", "of:catalog_offer:of_pg_ys"], ident
        [src] = _sources(res, "catalog_offers")
        assert src["status"] == "ok" and src["resolved_only"] == "on" and src["resolved"] == 3, (ident, src)


def test_an_unresolved_group_serves_the_named_listing_only_on_postgres(schema_engine, monkeypatch):  # noqa: F811
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc) - timedelta(days=1), resolved=False)
    off = _arm(monkeypatch, RETAILER_SIG, "off")
    assert _catalog_ids(off) == ["of:catalog_offer:of_pg_sk", "of:catalog_offer:of_pg_ys"], "off widens by key"
    # Named by its signature and by its product_key: each arm of the statement's own-listing match.
    for ident in (RETAILER_SIG, RETAILER_PK):
        res = _arm(monkeypatch, ident, "on")
        assert _catalog_ids(res) == ["of:catalog_offer:of_pg_sk"], ident
        [src] = _sources(res, "catalog_offers")
        assert src["status"] == "ok" and src["resolved"] == 0, (ident, src)
    # A bare key with no resolved member: an empty :resolved_keys list, which matches nothing.
    bare = _arm(monkeypatch, CK, "on")
    assert _catalog_ids(bare) == []
    [src] = _sources(bare, "catalog_offers")
    assert src["status"] == "empty", src


def test_shadow_answers_as_off_and_counts_on_postgres(schema_engine, monkeypatch):  # noqa: F811
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc) - timedelta(days=1), resolved=False)
    res = _arm(monkeypatch, RETAILER_SIG, "shadow")
    assert _catalog_ids(res) == ["of:catalog_offer:of_pg_sk", "of:catalog_offer:of_pg_ys"]
    [src] = _sources(res, "catalog_offers")
    assert src["resolved_only"] == "shadow" and src["would_withhold"] == 1 and src["would_withhold_listings"] == 1
