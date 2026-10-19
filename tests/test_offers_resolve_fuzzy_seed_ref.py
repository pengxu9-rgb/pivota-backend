"""The shape of offers.resolve's external_seed_by_fuzzy_ref statement (`_fuzzy_seed_ref_clauses`).

The plans and the same-rows proof run on Postgres in test_offers_resolve_fuzzy_seed_ref_postgres.py;
this file pins, on every engine, what those proofs rest on: which refs count as id-shaped, and that
every LIKE arm is spelled exactly as one of migration 270's index expressions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from routes.agent_shop_gateway import _fuzzy_seed_ref_clauses, _identity_title_arm, _is_id_shaped_ref

_MIGRATION_270 = (
    Path(__file__).resolve().parent.parent / "db/migrations/270_external_seed_active_any_trgm_indexes.sql.disabled"
)
_MIGRATION_271 = (
    Path(__file__).resolve().parent.parent / "db/migrations/271_external_seed_active_seed_title_trgm_index.sql.disabled"
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
    """Spell a query expression and an indexdef expression alike: Postgres prints CAST(x AS TEXT) as
    (x)::text, quotes a literal's type ('title'::text), and parenthesises an operator's operands."""
    expr = expr.lower().replace("cast(seed_data as text)", "(seed_data)::text")
    expr = expr.replace("'title'::text", "'title'").replace("''::text", "''")
    expr = expr.replace("(seed_data ->> 'title')", "seed_data->>'title'")
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


@pytest.mark.parametrize("path, count", [(_MIGRATION_270, 6), (_MIGRATION_271, 1)])
def test_the_seed_index_migrations_are_hand_applied_and_run_on_autocommit(path, count):
    from db.sql_migrations import is_hand_applied_migration, list_migration_files, needs_autocommit

    body = path.read_text()
    assert is_hand_applied_migration(str(path))
    assert str(path) not in list_migration_files()
    assert needs_autocommit(body)
    statements = re.findall(r"^CREATE INDEX (CONCURRENTLY IF NOT EXISTS )?(\w+)", body, re.M)
    assert all(flag for flag, _name in statements) and len(statements) == count
    # No '%' anywhere: a tool that binds parameters with the file body (psycopg2 with vars) reads one
    # as a placeholder.
    assert "%" not in body


# --- the identity retry's title arm (external_seed_by_internal_identity) ---------------------------


def test_a_normalized_title_term_gets_the_indexed_arms():
    arm = _identity_title_arm("identity_title_0", "kravebeauty great barrier relief")
    assert arm == ("(LOWER(title) LIKE :identity_title_0"
                   " OR LOWER(COALESCE(seed_data->>'title','')) LIKE :identity_title_0)")


@pytest.mark.parametrize("term", ["", "   ", "%", "50%", "a_b", "L'Oreal", "Great Barrier"])
def test_any_other_term_keeps_mains_arm(term):
    """Dropping COALESCE(title,'') is only the same rows when '' cannot match '%<term>%'; the
    normalizer never emits these, but if it ever did they keep main's arm exactly."""
    assert _identity_title_arm("k", term) == (
        "(LOWER(COALESCE(title,'')) LIKE :k OR LOWER(COALESCE(seed_data->>'title','')) LIKE :k)"
    )


def test_the_normalizer_only_emits_terms_that_get_the_indexed_arms():
    from routes.agent_shop_gateway import _IDENTITY_TERM_RE, _normalize_offer_title

    for raw in ("KraveBeauty Great Barrier Relief", "L'Oréal Paris Revitalift 1.5% Serum", "Dr.Jart+ Cicapair™",
                "SK-II Facial Treatment Essence (230ml)", "  ÉTUDE  House  ", "100% Pure_Glow"):
        term = _normalize_offer_title(raw)
        assert _IDENTITY_TERM_RE.fullmatch(term), (raw, term)


def test_both_identity_arms_are_spelled_as_an_index_expression():
    indexed = {_normalize(m) for m in re.findall(r"USING gin \((.+?) gin_trgm_ops\)",
                                                 _MIGRATION_270.read_text() + _MIGRATION_271.read_text())}
    arm = _identity_title_arm("t", "great barrier relief")
    sides = re.findall(r"(LOWER\(.+?\)) LIKE :t", arm)
    assert len(sides) == 2
    for side in sides:
        assert _normalize(side) in indexed, side

