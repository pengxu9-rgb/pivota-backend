"""What may and may not earn `pdp_scope='multi_merchant_canonical'`.

WHY THIS MATTERS. `services/pivot_query_service.py` grants that label a +200
search-rank bonus in THREE places (:1048, :1090, :1476), documented as "large
enough to dominate every other term" — above exact-SKU (120), exact-title (100)
and exact-brand (80). A row that earns the label on one seller outranks genuine
merchant listings on every matched query. `_promote_canonical_scopes` used to
grant it on `EXISTS (an active attached seed)`: ONE seed.

THIS FILE DRIVES THE REAL FUNCTION. Its first version imported the predicate
STRING and tested that. Nothing asserted the production UPDATE used it, and
nothing ever called `_promote_canonical_scopes`, so review put the verbatim
original bug back into the UPDATE and all ten tests stayed green — along with
`OR TRUE` (promote everything) and `AND 1 = 0` (promote nothing). A test pinned
to a constant sitting NEXT TO the code path is the same substitution error as
the proxies this predicate exists to remove.

IT ALSO DOES NOT CLAIM EQUIVALENCE WITH `classify()`. The classifier takes an
abstract `seller_count`; this SQL works from the signals actually on a row —
offers, attached seeds, product-group peers — which do not reduce to that
number. The first version claimed equivalence and, to make the claim come out
true, its oracle computed `max(offer_merchants, seed_domains)` where the
classifier documents a SUM. That oracle was the SQL's own semantics restated,
so the matrix was self-consistent by construction and blind to the one case
where the two genuinely disagree.

So these are PROPERTIES, named and measured: shapes that must not promote, and
shapes that must. Every one is mutation-checked.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from services.pdp_scope_classifier import LABEL_SOURCE_ENRICHMENT, SCOPE_CANONICAL

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SCHEMA = f"pdp_scope_test_{os.getpid()}"

# A PER-PROCESS SCHEMA, created fresh and dropped at teardown. Two earlier
# attempts were not enough:
#   * one shared schema -> two concurrent pytest processes ran the same case ids
#     and collided on the primary key. Review saw 5 failures in 6 concurrent
#     runs, some of them real assertion failures whose message reads exactly
#     like the defect under test. A flake that IMPERSONATES the bug is worse
#     than a noisy one — it teaches people to dismiss the real signal.
#   * per-case keys in a shared schema -> rows accumulated across runs (326
#     products after one mutation battery) and PIDs get reused, so keys
#     eventually collided anyway and the suite went red on its own.
# Owning the schema makes the fixture exact, concurrent-safe, and self-cleaning.
_RUN = str(os.getpid())

# (id, label, category_label_source, offers, seeds, group_peers, must_promote)
#   offers      : [merchant_id, ...]                      -> one catalog_offers row each
#   seeds       : [(domain, status), ...]                 -> attached to THIS product_key
#   group_peers : [(merchant_id, platform_product_id), ...] -> product_group_members
CASES = [
    # --- MUST NOT promote: the inflation shapes -----------------------------
    ("single_seed", "one active seed only", None, [], [("a.com", "active")], [], False),
    ("single_offer", "one offer merchant only", None, ["m1"], [], [], False),
    ("no_sellers", "no sellers at all", None, [], [], [], False),
    # Same seller, two ROWS. `count(DISTINCT ...)` is what stops these; review
    # found plain `count(...)` survived, i.e. these promoted.
    ("dup_offer_rows", "same merchant, TWO offer rows", None, ["m1", "m1"], [], [], False),
    ("dup_seed_rows", "same domain, TWO seed rows", None, [],
     [("a.com", "active"), ("a.com", "active")], [], False),
    # `status='active'` is load-bearing; dropping it survived review's mutation.
    ("inactive_seeds", "two INACTIVE seed domains", None, [],
     [("a.com", "disabled"), ("b.com", "disabled")], [], False),
    ("null_domain_seeds", "two active seeds with NULL domains", None, [],
     [(None, "active"), (None, "active")], [], False),

    # --- MUST promote: genuine multi-seller ---------------------------------
    ("two_offers", "two offer merchants", None, ["m1", "m2"], [], [], True),
    ("two_seed_domains", "two active seed domains", None, [],
     [("a.com", "active"), ("b.com", "active")], [], True),
    # ISOLATES the peer-merchant branch: the peer shares own's
    # platform_product_id (spid=None), so the bucket branch — which needs a
    # DIFFERENT one — cannot also fire. Review's mutation deleting the
    # peer-merchant branch survived because the old fixture tripped both.
    ("peer_other_merchant", "product-group peer at a DIFFERENT merchant, same spid",
     None, [], [], [("other_merchant", None)], True),
    # THE ext: COHORT. Deleting the bucket branch un-promoted this entire
    # cohort in review — 3 retailers on one ext: identity, promoted 3 -> 0.
    # Its seeds hang off an `ext:` key, not this row's, so the seed-domain
    # count sees zero; every member is merchant_id='external_seed', so the
    # peer-merchant branch cannot fire.
    ("ext_cohort", "bucket peer at a different source product (pg_ext_ cohort)",
     None, [], [], [("external_seed", "other_epid")], True),

    # --- Rule 1: agent-authored is canonical BY INTENT ----------------------
    ("agent_no_sellers", "agent-authored, no sellers", LABEL_SOURCE_ENRICHMENT,
     [], [], [], True),
    ("agent_one_seller", "agent-authored, one seller", LABEL_SOURCE_ENRICHMENT,
     ["m1"], [], [], True),
]


@pytest.fixture(scope="module")
def engine():
    from sqlalchemy import create_engine, text

    # Pin search_path on the CONNECTION, not via a stray `SET`. Review showed a
    # bare `SET` survives only because the pool happened to hand back the same
    # backend; on a second connection the tables silently did not resolve, and
    # against a database with a real `public.catalog_products` the same query
    # returned a DIFFERENT answer with no error.
    eng = create_engine(
        os.environ["DATABASE_URL"], future=True,
        connect_args={"options": f"-csearch_path={_SCHEMA}"})
    raw = create_engine(os.environ["DATABASE_URL"], future=True)
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    with eng.begin() as c:
        for ddl in (
            "CREATE TABLE IF NOT EXISTS catalog_products (product_key text PRIMARY KEY,"
            " merchant_id text, platform text, source_product_id text,"
            " category_label_source text, pdp_scope text, pdp_scope_source text,"
            " pdp_scope_set_at timestamptz)",
            "CREATE TABLE IF NOT EXISTS catalog_offers (product_key text, merchant_id text)",
            "CREATE TABLE IF NOT EXISTS external_product_seeds (id text,"
            " attached_product_key text, status text, domain text)",
            "CREATE TABLE IF NOT EXISTS product_group_members (product_group_id text,"
            " merchant_id text, platform text, platform_product_id text)",
        ):
            c.execute(text(ddl))
    yield eng
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


def _seed_fixture(conn, case_id, cls_source, offers, seeds, peers):
    """Per-case keys, so concurrent runs cannot collide.

    Review ran two pytest processes and got 5 failures in 6 — including real
    assertion failures whose message reads exactly like the defect under test.
    A flake that impersonates the bug trains people to dismiss the real signal.
    """
    from sqlalchemy import text

    pk, pg = f"pk_{case_id}_{_RUN}", f"pg_{case_id}_{_RUN}"
    conn.execute(text("DELETE FROM catalog_offers WHERE product_key = :pk"), {"pk": pk})
    conn.execute(text("DELETE FROM external_product_seeds WHERE attached_product_key = :pk"),
                 {"pk": pk})
    conn.execute(text("DELETE FROM product_group_members WHERE product_group_id = :pg"),
                 {"pg": pg})
    conn.execute(text("DELETE FROM catalog_products WHERE product_key = :pk"), {"pk": pk})

    conn.execute(text(
        "INSERT INTO catalog_products (product_key, merchant_id, platform,"
        " source_product_id, category_label_source, pdp_scope)"
        " VALUES (:pk,'external_seed','external_seed',:spid,:cls,'unverified')"),
        {"pk": pk, "spid": f"spid_{case_id}_{_RUN}", "cls": cls_source})
    for i, m in enumerate(offers):
        conn.execute(text("INSERT INTO catalog_offers (product_key, merchant_id)"
                          " VALUES (:pk, :m)"), {"pk": pk, "m": m})
    for i, (dom, status) in enumerate(seeds):
        conn.execute(text("INSERT INTO external_product_seeds (id, attached_product_key,"
                          " status, domain) VALUES (:i, :pk, :st, :d)"),
                     {"i": f"{case_id}_{_RUN}_s{i}", "pk": pk, "st": status, "d": dom})
    if peers:
        conn.execute(text("INSERT INTO product_group_members (product_group_id, merchant_id,"
                          " platform, platform_product_id)"
                          " VALUES (:pg,'external_seed','external_seed',:spid)"),
                     {"pg": pg, "spid": f"spid_{case_id}_{_RUN}"})
        for m, spid in peers:
            # spid=None means "same platform_product_id as own", which isolates
            # the peer-MERCHANT branch from the bucket branch.
            conn.execute(text("INSERT INTO product_group_members (product_group_id,"
                              " merchant_id, platform, platform_product_id)"
                              " VALUES (:pg, :m, 'external_seed', :spid)"),
                         {"pg": pg, "m": m, "spid": spid or f"spid_{case_id}_{_RUN}"})
    return pk


def _run_promotion(engine, product_key):
    """Drive the REAL `_promote_canonical_scopes`, not a copy of its SQL.

    It is `async` and talks to the module-global `database`, so point that at
    this fixture's engine for the call.
    """
    import databases

    from services import pdp_identity_recovery as mod

    url = str(engine.url)
    db = databases.Database(url.replace("postgresql://", "postgresql+asyncpg://")
                            if "+asyncpg" not in url else url,
                            server_settings={"search_path": _SCHEMA})

    async def _go():
        await db.connect()
        try:
            return await mod._promote_canonical_scopes([product_key])
        finally:
            await db.disconnect()

    original = mod.database
    mod.database = db
    try:
        return asyncio.run(_go())
    finally:
        mod.database = original


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_only_genuine_multi_seller_shapes_are_promoted(engine, case):
    from sqlalchemy import text

    case_id, label, cls_source, offers, seeds, peers, must_promote = case

    with engine.begin() as c:
        pk = _seed_fixture(c, case_id, cls_source, offers, seeds, peers)

    _run_promotion(engine, pk)

    with engine.begin() as c:
        scope = c.execute(text("SELECT pdp_scope FROM catalog_products"
                               " WHERE product_key = :pk"), {"pk": pk}).scalar()

    promoted = scope == SCOPE_CANONICAL
    assert promoted == must_promote, (
        f"{label}: pdp_scope={scope!r}, expected "
        f"{'promotion' if must_promote else 'NO promotion'}. "
        "The label carries a +200 search-rank bonus that dominates exact-match "
        "signals, so an over-promotion outranks real merchant listings.")


def test_the_production_update_uses_the_shared_predicate():
    """The UPDATE must interpolate CANONICAL_SCOPE_PREDICATE, not a copy.

    The parametrised tests above drive the real function, so this is belt and
    braces — but it is cheap and it names the invariant directly.
    """
    import inspect

    from services import pdp_identity_recovery as mod

    src = inspect.getsource(mod._promote_canonical_scopes)
    assert "{predicate}" in src
    assert "CANONICAL_SCOPE_PREDICATE" in src
    assert "EXISTS (\n              SELECT 1\n              FROM external_product_seeds eps" \
        not in mod.CANONICAL_SCOPE_PREDICATE.replace("  ", "  "), \
        "the bare active-seed branch is back — that is the original defect"
