"""What may and may not earn `pdp_scope='multi_merchant_canonical'`.

THE FILENAME IS LOAD-BEARING. `.github/workflows/postgres-dialect-gate.yml`
discovers `tests/test_*_postgres.py` by glob and runs those against a real
Postgres 15 service. This file was originally named
`test_pdp_scope_promotion_agrees_with_classifier.py`, which matches no glob —
and `tests/conftest.py` defaults DATABASE_URL to SQLite when unset, so the
module-level skipif fired and ALL of it skipped on the default CI path. A
prevention mechanism that does not execute prevents nothing. Do not rename
this file out of the `_postgres.py` suffix.

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

# (id, label, merchant, category_label_source, offers, seeds, peers, must_promote)
#   merchant : catalog_products.merchant_id — 'external_seed' is THE BUCKET, any
#              other value is a real merchant. Previously hardcoded to the
#              bucket, which made the bucket/non-bucket distinction
#              inexpressible and left three mutations undetectable.
#   offers   : [merchant_id, ...]                      -> one catalog_offers row each
#   seeds    : [(domain, status), ...]                 -> attached to THIS product_key
#   peers    : [(merchant_id, platform_product_id), ...]  spid None = same as own
#   cluster_seeds : [(external_product_id, domain), ...] — seeds reached via the
#              product GROUP rather than attached_product_key (the pg_ext_ shape)
_BUCKET = "external_seed"
_REAL = "acme_shop"

_RAW_CASES = [
    # --- MUST NOT promote ---------------------------------------------------
    # In the BUCKET the own merchant is not a seller, so one seed is 1.
    ("bucket_single_seed", "bucket row, one active seed", _BUCKET, None,
     [], [("a.com", "active")], [], False),
    ("bucket_single_offer", "bucket row, one offer merchant", _BUCKET, None,
     ["m1"], [], [], False),
    ("bucket_no_sellers", "bucket row, no sellers", _BUCKET, None, [], [], [], False),
    ("real_no_sellers", "real merchant, no offers and no seeds", _REAL, None,
     [], [], [], False),
    # Same seller, two ROWS — `count(DISTINCT ...)` is what stops these.
    ("dup_offer_rows", "bucket row, same merchant TWO offer rows", _BUCKET, None,
     ["m1", "m1"], [], [], False),
    ("dup_seed_rows", "bucket row, same domain TWO seed rows", _BUCKET, None,
     [], [("a.com", "active"), ("a.com", "active")], [], False),
    ("inactive_seeds", "bucket row, two INACTIVE seed domains", _BUCKET, None,
     [], [("a.com", "disabled"), ("b.com", "disabled")], [], False),
    ("null_domain_seeds", "bucket row, two active seeds with NULL domains", _BUCKET,
     None, [], [(None, "active"), (None, "active")], [], False),
    # --- negative coverage for the product-group branches -------------------
    # Added after review: all three mutations below survived because NO negative
    # case had a product_group_members row at all.
    ("group_of_one", "bucket row alone in its group", _BUCKET, None,
     [], [], [], False),
    ("peer_same_merchant_same_spid", "peer identical to own (duplicate group row)",
     _BUCKET, None, [], [], [(_BUCKET, None)], False),
    # THE bucket-gate case: a REAL merchant with a peer at a different product
    # id but the SAME merchant. Dropping branch (e)'s `merchant_id='external_seed'`
    # gate promotes this on one seller; nothing detected that before.
    ("real_peer_diff_spid_same_merchant",
     "real merchant, peer at a different product id, same merchant", _REAL, None,
     [], [], [(_REAL, "other_spid")], False),
    # PINS THE BUCKET GATE on branch (e). Same shape as the ext: cohort — a peer
    # at a different product id, cluster seeds on TWO domains — but at a REAL
    # merchant, so branch (e) must NOT apply. Without this case, deleting the
    # `cp.merchant_id = 'external_seed'` gate is undetectable: every other
    # non-bucket case lacks cluster seeds, so the domain count is 0 and the
    # branch stays false for the wrong reason.
    ("real_cluster_two_domains_not_bucket",
     "REAL merchant with a two-domain cluster — branch (e) must not apply",
     _REAL, None, [], [], [(_REAL, "other_spid")], False,
     [("SPID", "zappos.com"), ("other_spid", "nordstrom.com")]),
    # Bucket cluster whose seeds are all on ONE domain — one retailer, two SKUs.
    # The old `platform_product_id <>` gate promoted this; the domain gate does not.
    ("bucket_cluster_one_domain", "bucket cluster, two product ids, ONE domain",
     _BUCKET, None, [], [], [(_BUCKET, "other_epid")], False,
     [("SPID", "zappos.com"), ("other_epid", "zappos.com")]),

    # --- MUST promote -------------------------------------------------------
    # A REAL merchant is itself a seller: own(1) + one seed domain(1) = 2.
    # Deleting the old active-seed branch globally broke exactly this shape.
    ("real_plus_one_seed", "real merchant + one active seed = 2 sellers", _REAL,
     None, [], [("zappos.com", "active")], [], True),
    ("real_plus_one_offer", "real merchant + one OTHER offer merchant = 2", _REAL,
     None, ["other_m"], [], [], True),
    ("bucket_two_offers", "bucket row, two offer merchants", _BUCKET, None,
     ["m1", "m2"], [], [], True),
    ("bucket_two_seed_domains", "bucket row, two active seed domains", _BUCKET,
     None, [], [("a.com", "active"), ("b.com", "active")], [], True),
    # Isolates branch (d): peer shares own's spid, so the bucket branch cannot fire.
    ("peer_other_merchant", "peer at a DIFFERENT merchant, same spid", _BUCKET,
     None, [], [], [("other_merchant", None)], True),
    # THE ext: COHORT, correctly gated: two product ids on TWO retailer domains.
    ("ext_cohort_two_domains", "bucket cluster, two product ids, TWO domains",
     _BUCKET, None, [], [], [(_BUCKET, "other_epid")], True,
     [("SPID", "zappos.com"), ("other_epid", "nordstrom.com")]),

    # --- Rule 1 -------------------------------------------------------------
    ("agent_no_sellers", "agent-authored, no sellers", _BUCKET,
     LABEL_SOURCE_ENRICHMENT, [], [], [], True),
]

# Normalise to 9 fields; cluster_seeds defaults to none.
CASES = [c if len(c) == 9 else (*c, ()) for c in _RAW_CASES]


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
            " attached_product_key text, status text, domain text,"
            " external_product_id text)",
            "CREATE TABLE IF NOT EXISTS product_group_members (product_group_id text,"
            " merchant_id text, platform text, platform_product_id text)",
        ):
            c.execute(text(ddl))
    yield eng
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


def _seed_fixture(conn, case_id, merchant, cls_source, offers, seeds, peers,
                  cluster_seeds=()):
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
        " VALUES (:pk,:m,'external_seed',:spid,:cls,'unverified')"),
        {"pk": pk, "m": merchant, "spid": f"spid_{case_id}_{_RUN}", "cls": cls_source})
    for i, m in enumerate(offers):
        conn.execute(text("INSERT INTO catalog_offers (product_key, merchant_id)"
                          " VALUES (:pk, :m)"), {"pk": pk, "m": m})
    for i, (dom, status) in enumerate(seeds):
        conn.execute(text("INSERT INTO external_product_seeds (id, attached_product_key,"
                          " status, domain) VALUES (:i, :pk, :st, :d)"),
                     {"i": f"{case_id}_{_RUN}_s{i}", "pk": pk, "st": status, "d": dom})
    # Cluster seeds carry NO attached_product_key — they are reached only through
    # the group, via external_product_id = a member's platform_product_id.
    for i, (epid, dom) in enumerate(cluster_seeds):
        real_epid = f"spid_{case_id}_{_RUN}" if epid == "SPID" else epid
        conn.execute(text("INSERT INTO external_product_seeds (id, status, domain,"
                          " external_product_id) VALUES (:i,'active',:d,:e)"),
                     {"i": f"{case_id}_{_RUN}_c{i}", "d": dom, "e": real_epid})
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

    # render_as_string(hide_password=False): SQLAlchemy 2.0 renders the
    # password as "***" in str(url), and `databases` then sends the literal
    # "***". Review found this pinned the whole file to a passwordless local
    # socket — it errored on connect against CI, staging, and the prod proxy.
    url = engine.url.render_as_string(hide_password=False)
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

    (case_id, label, merchant, cls_source, offers, seeds, peers, must_promote,
     cluster_seeds) = case

    with engine.begin() as c:
        pk = _seed_fixture(c, case_id, merchant, cls_source, offers, seeds, peers,
                           cluster_seeds)

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
