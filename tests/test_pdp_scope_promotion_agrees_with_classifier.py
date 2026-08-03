"""The scope-promotion SQL and `pdp_scope_classifier.classify` are ONE rule.

WHY THIS FILE EXISTS. `pdp_scope` has two implementations:

  * `services/pdp_scope_classifier.classify` — the pure, documented rule;
  * `services/pdp_identity_recovery._promote_canonical_scopes` — the SQL that
    actually writes the column in production.

They diverged, silently, in the direction that inflates. The SQL promoted on
`EXISTS (an active attached seed)` — ONE seed — and, for `external_seed` rows,
on a peer with a different `platform_product_id`, using "different product id"
as a proxy for "different merchant".

Both proxies were compensating for the same root cause: the `external_seed`
bucket collapses many real merchants into one `merchant_id`, which makes the
CORRECT test (`peer.merchant_id <> own.merchant_id`) structurally unsatisfiable
for rows inside it. Weaker stand-ins were substituted for the erased signal.

Measured on prod 2026-08-03: 3,293 mirror-lane rows were labelled
`multi_merchant_canonical` with exactly ONE seller — and that label carries a
+200 search-rank bonus documented as dominating every other term, so those rows
outranked genuine merchant listings on any matched query.

Nothing detected it, because nothing compared the two implementations. That is
the same shape as the seven-way domain comparator
(`tests/test_quarantine_comparator_agreement.py`) and the byte-identical SQL
pins between the two repos: where one rule has two implementations, the ONLY
defense that has held in this codebase is a test that runs both and diffs them.

These run on real Postgres — the promotion is Postgres-dialect SQL and SQLite
cannot gate it.
"""

from __future__ import annotations

import os

import pytest

from services.pdp_scope_classifier import (
    SCOPE_CANONICAL,
    SCOPE_MERCHANT_OWNED,
    ScopeSignals,
    classify,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)


# (label, category_label_source, offer_merchants, active_seed_domains,
#  peer_merchant_differs)
FIXTURES = [
    # The defect: one active seed is NOT multi-merchant.
    ("one active seed only", None, [], ["a.com"], False),
    ("one offer merchant only", None, ["m1"], [], False),
    ("no sellers at all", None, [], [], False),
    # Genuine multi-seller, by each available route.
    ("two offer merchants", None, ["m1", "m2"], [], False),
    ("two seed domains", None, [], ["a.com", "b.com"], False),
    ("peer at a different merchant", None, ["m1"], [], True),
    # Rule 1 — agent-authored is canonical by intent, whatever the count.
    ("agent-authored, no sellers", "enrichment_agent_v1", [], [], False),
    ("agent-authored, one seller", "enrichment_agent_v1", ["m1"], [], False),
    # The proxy that used to fire: same bucket merchant, different product id.
    ("bucket peer, different product_id, one seller", None, ["m1"], ["a.com"], False),
]


def _expected(cls_source, offer_merchants, seed_domains, peer_differs) -> str:
    """What the pure classifier says. `peer at a different merchant` is a
    product-group route to the same seller_count >= 2 fact."""
    seller_count = max(len(set(offer_merchants)), len(set(seed_domains)))
    if peer_differs:
        seller_count = max(seller_count, 2)
    return classify(ScopeSignals(
        category_label_source=cls_source, seller_count=seller_count))


@pytest.fixture(scope="module")
def pg():
    from sqlalchemy import create_engine

    eng = create_engine(os.environ["DATABASE_URL"], future=True)
    with eng.begin() as c:
        from sqlalchemy import text

        # A DEDICATED SCHEMA, as test_quarantine_domain_chain_postgres does.
        # The gate files share ONE database (#1651): the real `catalog_products`
        # there carries NOT NULL columns this fixture has no business supplying,
        # and a minimal `CREATE TABLE IF NOT EXISTS` silently keeps whichever
        # shape ran first. An owned schema makes the fixture exact and cannot
        # disturb a sibling file.
        c.execute(text("DROP SCHEMA IF EXISTS pdp_scope_test CASCADE"))
        c.execute(text("CREATE SCHEMA pdp_scope_test"))
        c.execute(text("SET search_path TO pdp_scope_test"))
        for ddl in (
            "CREATE TABLE IF NOT EXISTS catalog_products (product_key text PRIMARY KEY,"
            " merchant_id text, platform text, source_product_id text,"
            " category_label_source text, pdp_scope text, pdp_scope_source text,"
            " pdp_scope_set_at timestamptz, suppressed_at timestamptz)",
            "ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS category_label_source text",
            "ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_scope text",
            "ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_scope_source text",
            "ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_scope_set_at timestamptz",
            "CREATE TABLE IF NOT EXISTS catalog_offers (product_key text, merchant_id text)",
            "CREATE TABLE IF NOT EXISTS external_product_seeds (id text,"
            " attached_product_key text, status text, domain text)",
            "CREATE TABLE IF NOT EXISTS product_group_members (product_group_id text,"
            " merchant_id text, platform text, platform_product_id text)",
        ):
            c.execute(text(ddl))
    return eng


def _promotion_sql() -> str:
    """The SHIPPED predicate, IMPORTED — not retyped and not scraped.

    Retyping it would test a copy, which is the failure this file exists to
    prevent. Scraping the module source was the first attempt and it broke
    silently when the SQL gained an interpolation: the lifted text still
    contained a literal `{...}` placeholder and two fixtures went red.
    """
    from services.pdp_identity_recovery import CANONICAL_SCOPE_PREDICATE

    return "WHERE TRUE AND (\n" + CANONICAL_SCOPE_PREDICATE + "\n)"


@pytest.mark.parametrize("case", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_the_sql_promotion_matches_the_pure_classifier(pg, case):
    from sqlalchemy import text

    label, cls_source, offer_merchants, seed_domains, peer_differs = case
    expected = _expected(cls_source, offer_merchants, seed_domains, peer_differs)

    with pg.begin() as c:
        # Scoped deletes: these gate files SHARE one database (#1651), so a
        # bare `DELETE FROM` would wipe a sibling file's fixtures.
        c.execute(text("DELETE FROM catalog_offers WHERE product_key='pk'"))
        c.execute(text("DELETE FROM external_product_seeds WHERE attached_product_key='pk'"))
        c.execute(text("DELETE FROM product_group_members WHERE product_group_id='pg1'"))
        c.execute(text("DELETE FROM catalog_products WHERE product_key='pk'"))
        c.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform,"
            " source_product_id, category_label_source) VALUES"
            " ('pk','external_seed','external_seed','spid', :cls)"), {"cls": cls_source})
        for m in offer_merchants:
            c.execute(text("INSERT INTO catalog_offers (product_key, merchant_id) VALUES ('pk', :m)"), {"m": m})
        for i, d in enumerate(seed_domains):
            c.execute(text("INSERT INTO external_product_seeds (id, attached_product_key,"
                           " status, domain) VALUES (:i,'pk','active',:d)"), {"i": f"s{i}", "d": d})
        # `own` always exists; the peer's merchant is what varies.
        c.execute(text("INSERT INTO product_group_members (product_group_id, merchant_id,"
                       " platform, platform_product_id)"
                       " VALUES ('pg1','external_seed','external_seed','spid')"))
        c.execute(text("INSERT INTO product_group_members (product_group_id, merchant_id,"
                       " platform, platform_product_id)"
                       " VALUES ('pg1', :peer, 'external_seed', 'other_spid')"),
                  {"peer": "other_merchant" if peer_differs else "external_seed"})

        promoted = c.execute(text(
            f"SELECT count(*) FROM catalog_products cp {_promotion_sql()}")).scalar()

    actual = SCOPE_CANONICAL if promoted else SCOPE_MERCHANT_OWNED
    assert actual == expected, (
        f"{label}: SQL promotion says {actual}, classifier says {expected}. "
        "These are two implementations of ONE rule and must agree — the last "
        "divergence labelled 3,293 single-seller rows canonical and handed each "
        "a +200 search-rank bonus.")


def test_a_single_active_seed_is_not_multi_merchant(pg):
    """The exact defect, called out on its own so it cannot regress quietly."""
    from sqlalchemy import text

    with pg.begin() as c:
        # Scoped deletes: these gate files SHARE one database (#1651), so a
        # bare `DELETE FROM` would wipe a sibling file's fixtures.
        c.execute(text("DELETE FROM catalog_offers WHERE product_key='pk'"))
        c.execute(text("DELETE FROM external_product_seeds WHERE attached_product_key='pk'"))
        c.execute(text("DELETE FROM product_group_members WHERE product_group_id='pg1'"))
        c.execute(text("DELETE FROM catalog_products WHERE product_key='pk'"))
        c.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform,"
            " source_product_id) VALUES ('pk','external_seed','external_seed','spid')"))
        c.execute(text("INSERT INTO external_product_seeds (id, attached_product_key,"
                       " status, domain) VALUES ('s1','pk','active','only-one.com')"))
        promoted = c.execute(text(
            f"SELECT count(*) FROM catalog_products cp {_promotion_sql()}")).scalar()

    assert promoted == 0, (
        "one active attached seed promoted a row to multi_merchant_canonical — "
        "this is the predicate that mislabelled 3,293 prod rows")
