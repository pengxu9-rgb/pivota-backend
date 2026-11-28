"""The catalog arm's resolved-only statement (OFFERS_CATALOG_ARM_RESOLVED_ONLY) on POSTGRES.

tests/test_offers_catalog_arm_resolved_only.py proves the behaviour with the reads faked by SQL shape.
This file sends the resolved-only statement through the REAL handler to a real Postgres: its new
`d.product_key = ANY(:resolved_keys)` arm with a list bind, an empty one included. The group-sibling Postgres file's scratch schema and rows are reused (three listings on one
content_key, resolved by applied same-size moves, or not).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from tests.test_offers_resolve_group_shadow_postgres import _fresh_shadow_cache, _run  # noqa: F401 (autouse fixture)
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


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list = []

    def emit(self, record: logging.LogRecord) -> None:
        name, _, payload = record.getMessage().partition(" ")
        if name == "offers_catalog_arm_shadow":
            self.lines.append(json.loads(payload))


def _shadow(monkeypatch, ident):
    """The REAL handler in shadow mode, its background tasks run as Starlette runs them, then the
    detached shadow drained: (response, the shadow's lines)."""
    handler = _Lines()
    pivota = logging.getLogger("pivota")
    pivota.addHandler(handler)
    monkeypatch.setenv(ARM_FLAG, "shadow")

    async def body(h):
        res, bt = await h.call(ident, flag=None)
        await h.settle(bt)
        return res

    try:
        return _run(monkeypatch, body), handler.lines
    finally:
        pivota.removeHandler(handler)


def test_shadow_answers_as_off_and_counts_after_the_response_on_postgres(schema_engine, monkeypatch):  # noqa: F811
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc) - timedelta(days=1), resolved=False)
    # Named by its signature and by its product_key: each arm of the shadow's named-listings read.
    for ident in (RETAILER_SIG, RETAILER_PK):
        res, lines = _shadow(monkeypatch, ident)
        assert _catalog_ids(res) == ["of:catalog_offer:of_pg_sk", "of:catalog_offer:of_pg_ys"], "the off answer"
        [src] = _sources(res, "catalog_offers")
        assert src["resolved_only"] == "shadow" and "would_withhold" not in src
        [line] = lines
        assert "error" not in line, (ident, line)
        assert (line["rows"], line["named"], line["resolved"]) == (2, 1, 0), (ident, line)
        assert line["would_withhold"] == 1 and line["would_withhold_listings"] == 1, (ident, line)
        # its reads, all after the response: the named listings, the key's listings, the engine's moves
        assert line["shadow_statements"] == 3, (ident, line)


def test_shadow_counts_nothing_withheld_for_a_resolved_group_on_postgres(schema_engine, monkeypatch):  # noqa: F811
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc) - timedelta(days=1))
    for ident in (RETAILER_SIG, CK):
        res, lines = _shadow(monkeypatch, ident)
        [line] = lines
        assert line["resolved"] == 3 and line["would_withhold"] == 0, (ident, line)
