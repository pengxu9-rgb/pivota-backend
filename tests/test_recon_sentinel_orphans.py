"""scripts/recon_sentinel_orphans.py — READ-ONLY recon over the sentinel residue.

A stateful fake DB that (a) asserts exact bind equality on every statement
(`databases` rejects both extra and missing binds), (b) refuses any write, and
(c) answers information_schema / residue counts from a declared schema, so the
control flow — which tables get classified, by which key, and which inputs
abort — is pinned without a Postgres.
"""

from __future__ import annotations

import re

import pytest

from scripts import recon_sentinel_orphans as recon


def _assert_binds_match(sql, params):
    named = set(re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", sql))
    given = set((params or {}).keys())
    assert named == given, (
        f"bind mismatch: SQL names {sorted(named)}, params give {sorted(given)}\nSQL: {sql}")


class FakeDB:
    """schema: {table: [(column, data_type)]}; residue: {table: n}."""

    def __init__(self, schema, residue):
        self.schema = schema
        self.residue = residue
        self.statements = []

    async def execute(self, sql, params=None):  # pragma: no cover - must never run
        raise AssertionError(f"recon wrote: {sql}")

    async def fetch_all(self, sql, params=None):
        _assert_binds_match(sql, params)
        flat = " ".join(sql.split())
        self.statements.append((flat, dict(params or {})))
        if "FROM information_schema.columns c" in flat:  # verifier's SELLER_COLUMNS_SQL
            out = []
            for t, cols in self.schema.items():
                names = {c for c, _ in cols}
                for c, dt in cols:
                    if c in ("merchant_id", "primary_merchant_id") and dt in (
                            "text", "character varying") and t != "catalog_merchants":
                        out.append({"table_name": t, "column_name": c,
                                    "product_scoped": bool(
                                        names & {"product_key", "content_key", "sku_key"})})
            return out
        if "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :table" in flat:
            return [{"column_name": c, "data_type": dt, "is_nullable": "YES"}
                    for c, dt in self.schema[params["table"]]]
        m = re.search(r"FROM (\w+) t", flat) or re.search(r"FROM (\w+) (?:pe|l)\b", flat) \
            or re.search(r"FROM (\w+) WHERE merchant_id = :banned", flat)
        assert m, f"unexpected fetch_all: {flat}"
        table = m.group(1)
        # classification GROUP BYs, scope-null count, key-space rate, samples:
        # one synthetic row each. The two count queries are distinguished by
        # their projections — answering both with one shape would let a swap
        # between them go unnoticed here.
        if "resolves" in flat:
            return [{"total": self.residue[table], "scoped": self.residue[table],
                     "resolves": self.residue[table]}]
        if "count(*) AS total" in flat:
            return [{"total": self.residue[table], "scope_null": 0}]
        return [{"n": self.residue[table], "marker": table}]

    async def fetch_one(self, sql, params=None):
        _assert_binds_match(sql, params)
        flat = " ".join(sql.split())
        self.statements.append((flat, dict(params or {})))
        m = re.match(r"SELECT count\(\*\) AS c FROM (\w+) WHERE (\w+) = :banned", flat)
        assert m, f"unexpected fetch_one: {flat}"
        return {"c": self.residue.get(m.group(1), 0)}


TEXT, INT = "text", "integer"
SCHEMA = {
    "catalog_merchants": [("merchant_id", TEXT)],
    "catalog_products": [("product_key", TEXT), ("content_key", TEXT), ("merchant_id", TEXT)],
    "catalog_offers": [("product_key", TEXT), ("merchant_id", TEXT), ("created_at", "timestamptz")],
    "index_pipeline_state": [("content_key", TEXT), ("merchant_id", TEXT)],
    "some_sku_table": [("sku_key", TEXT), ("merchant_id", TEXT)],
    "niche_target_outcomes": [("merchant_id", TEXT), ("content_key", TEXT), ("seen_at", "timestamptz")],
    "agent_product_events": [("merchant_id", TEXT), ("product_id", TEXT)],
    "pdp_identity_listing": [("source_listing_ref", TEXT), ("product_id", TEXT), ("merchant_id", TEXT)],
    "product_enrichment": [("merchant_id", "character varying"), ("platform", TEXT),
                           ("platform_product_id", TEXT)],
    "int_merchant_table": [("merchant_id", INT), ("product_key", TEXT)],
}
RESIDUE = {"catalog_products": 0, "catalog_offers": 12, "index_pipeline_state": 9,
           "some_sku_table": 3, "niche_target_outcomes": 317, "agent_product_events": 150000,
           "pdp_identity_listing": 103, "product_enrichment": 8, "int_merchant_table": 99}


def _run(db, also=("pdp_identity_listing", "product_enrichment"), sample=2):
    import asyncio
    return asyncio.run(recon.recon(db, also_history=list(also), sample=sample))


def test_classifies_every_ownership_table_by_its_scope_and_only_named_history_tables():
    db = FakeDB(SCHEMA, RESIDUE)
    out = _run(db)
    tables = out["tables"]
    assert set(tables) == {"catalog_offers", "index_pipeline_state", "some_sku_table",
                           "niche_target_outcomes", "pdp_identity_listing", "product_enrichment"}
    # history tables not named in --also-history are reported by the verifier,
    # never row-classified here (150k events would be scanned for nothing).
    assert "agent_product_events" not in tables
    # the INTEGER merchant_id table carries no seller identity — never targeted
    assert "int_merchant_table" not in tables
    assert "by_product" in tables["catalog_offers"]
    assert "by_content" in tables["index_pipeline_state"]
    assert "by_sku" in tables["some_sku_table"]
    assert "by_content" in tables["niche_target_outcomes"]
    assert tables["pdp_identity_listing"]["verifier_bucket"] == "history"
    assert "by_source_id" in tables["pdp_identity_listing"]
    assert "by_source_id" in tables["product_enrichment"]
    assert tables["product_enrichment"]["scope_col"] is None
    for t, rep in tables.items():
        assert rep["residue"] == RESIDUE[t]
        assert rep["sample"], t


def test_every_statement_binds_the_banned_sentinel_and_the_flip_phase_never_a_literal():
    db = FakeDB(SCHEMA, RESIDUE)
    _run(db)
    classify = [(s, p) for s, p in db.statements
                if "a9_4_backfill_checkpoint" in s]
    assert classify, "classification joins never ran"
    for s, p in classify:
        assert "banned" in p and "phase" in p  # bound, never inlined
        # The LITERAL, not recon.CHECKPOINT_PHASE — an assertion that reads the
        # constant it claims to pin passes for every value the constant takes.
        assert p["phase"] == "catalog"
        assert "'external_seed'" not in s and "'catalog'" not in s.replace(
            "ck.phase = :phase", "")


def test_unknown_history_table_aborts_instead_of_silently_skipping():
    db = FakeDB(SCHEMA, RESIDUE)
    with pytest.raises(RuntimeError, match="no text seller column"):
        _run(db, also=("int_merchant_table",))
    with pytest.raises(RuntimeError, match="no text seller column"):
        _run(db, also=("no_such_table",))


def test_history_table_named_but_with_zero_residue_is_skipped_not_scanned():
    residue = dict(RESIDUE, product_enrichment=0)
    db = FakeDB(SCHEMA, residue)
    out = _run(db)
    assert "product_enrichment" not in out["tables"]


def test_identifier_guard_refuses_injection_shaped_names():
    for bad in ("catalog_offers; DROP TABLE x", "Bad-Name", "", 'a"b'):
        with pytest.raises(RuntimeError):
            recon._ident(bad)
    assert recon._ident("catalog_offers") == "catalog_offers"
