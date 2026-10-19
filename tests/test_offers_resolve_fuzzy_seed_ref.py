"""The shape of offers.resolve's external_seed_by_fuzzy_ref statement (`_fuzzy_seed_ref_clauses`).

The plans and the same-rows proof run on Postgres in test_offers_resolve_fuzzy_seed_ref_postgres.py;
this file pins, on every engine, what those proofs rest on: which refs count as id-shaped, and that
every LIKE arm is spelled exactly as one of migration 270's index expressions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from routes.agent_shop_gateway import _fuzzy_seed_ref_clauses, _is_id_shaped_ref

_MIGRATION_270 = (
    Path(__file__).resolve().parent.parent / "db/migrations/270_external_seed_active_any_trgm_indexes.sql.disabled"
)


@pytest.mark.parametrize("ref", [
    "sig_" + "0123456789abcdef" * 2,
    "SIG_" + "0123456789ABCDEF" * 2,
    "ck_" + "a" * 32,
    "irp_" + "b" * 32,
    "ext_0f95730ee5ba05a6b7957ada",
    "7d0c6a8e-3b9f-4c1a-9e2d-5f4b3a2c1d0e",
    "  sig_" + "c" * 32 + "  ",
])
def test_id_shaped_refs(ref):
    assert _is_id_shaped_ref(ref)


@pytest.mark.parametrize("ref", [
    "ck_100",                      # %ck_100% matches the title "Pack 100ml": `_` is a LIKE wildcard
    "sig_pg_grp_brand",            # not a minted signature
    "sig_" + "a" * 31,             # one short
    "sig_" + "a" * 33,             # one long
    "ext_" + "a" * 32,             # ext_ is 24 hex
    "ext:retailer:pg-serum-sk",    # a product key, not an id
    "prod::m::shopify::123",
    "49819267301653",              # a Shopify id: may sit in a title ("... 49819267301653")
    "gid://shopify/Product/123",
    "silky matte lip ink",
    "sig_" + "g" * 32,             # not hex
    "",
])
def test_refs_that_keep_the_title_arm(ref):
    assert not _is_id_shaped_ref(ref)


def test_an_id_shaped_ref_skips_only_the_title_arm():
    sig = "sig_" + "3f" * 16
    [clause], params = _fuzzy_seed_ref_clauses([sig, "Silky Matte"], [])
    first, second = clause[1:-1].split(") OR (")
    assert "title" not in first
    for arm in ("external_product_id = :pid_0", "LOWER(canonical_url) LIKE :pid_like_0",
                "LOWER(destination_url) LIKE :pid_like_0", "LOWER(CAST(seed_data AS TEXT)) LIKE :pid_like_0"):
        assert arm in first
    assert "LOWER(title) LIKE :pid_like_1" in second
    assert params == {"pid_0": sig, "pid_like_0": f"%{sig}%", "pid_1": "Silky Matte", "pid_like_1": "%silky matte%"}


def test_no_aliases_no_clauses_and_the_alias_caps_hold():
    assert _fuzzy_seed_ref_clauses([], []) == ([], {})
    clauses, params = _fuzzy_seed_ref_clauses([f"a{i}" for i in range(12)], [f"s{i}" for i in range(12)])
    assert len(clauses) == 2
    assert sorted(k for k in params if k.startswith("pid_like_")) == [f"pid_like_{i}" for i in range(8)]
    assert sorted(k for k in params if k.startswith("sku_like_")) == [f"sku_like_{i}" for i in range(8)]


def _normalize(expr: str) -> str:
    expr = expr.lower().replace("cast(seed_data as text)", "(seed_data)::text")
    return re.sub(r"\s+", "", expr)


def test_every_like_arm_is_spelled_as_a_migration_270_index_expression():
    """A planner uses an expression index only for the SAME expression: LOWER(COALESCE(title,''))
    against gin (lower(title)) is a full-table scan (the 6.7-7.3 s prod measured 2026-10-19)."""
    body = _MIGRATION_270.read_text()
    indexed = {_normalize(m) for m in re.findall(r"USING gin \((.+?) gin_trgm_ops\)", body)}
    assert indexed >= {"lower(title)", "lower(canonical_url)", "lower(destination_url)", "lower((seed_data)::text)"}
    assert "USING btree (external_product_id)" in body
    clauses, _ = _fuzzy_seed_ref_clauses(["Silky Matte"], ["6978647800885"])
    arms = re.findall(r"(LOWER\(.+?\)) LIKE :", " ".join(clauses))
    assert len(arms) == 5
    for arm in arms:
        assert _normalize(arm) in indexed, arm
    assert "COALESCE" not in " ".join(clauses)
    # Every index is partial on status = 'active' alone, which the statement always carries; the
    # 051/068 ones (AND attached_product_key IS NULL) cover 5 of prod's 26,950 active rows.
    assert body.count("WHERE (status = 'active'::text);") == 6
    assert "attached_product_key IS NULL" not in body.split("schema-guard-exempt")[1]


def test_migration_270_is_hand_applied_and_runs_on_autocommit():
    from db.sql_migrations import is_hand_applied_migration, list_migration_files, needs_autocommit

    assert is_hand_applied_migration(str(_MIGRATION_270))
    assert str(_MIGRATION_270) not in list_migration_files()
    assert needs_autocommit(_MIGRATION_270.read_text())
    statements = re.findall(r"CREATE INDEX (CONCURRENTLY IF NOT EXISTS )?(\w+)", _MIGRATION_270.read_text())
    assert all(flag for flag, _name in statements) and len(statements) == 6
