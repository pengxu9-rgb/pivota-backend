"""Production-dialect gate for the Shopify variant-id backfill.

WHY THIS FILE EXISTS. Three of this script's four historical P0s were SQL semantics that no
Python-level test could see, because the suite's fake statement-executor applies bound
parameters without ever evaluating SQL:

  * `SET seed_data = ...` replaced the whole document when a row arrived as a JSON string;
  * `databases.execute()` is `fetchval`, so a non-RETURNING UPDATE returned None whether it
    wrote one row or none — every success was counted as a conflict;
  * `jsonb_typeof(x) = 'array'` as a SIBLING qual did not protect `jsonb_array_length(x)`,
    because Postgres reorders quals — one malformed row aborted the entire run.

The last one is the reason a text assertion is not enough here: the string was present and
correct, and the statement still raised. So this module EXECUTES the real statements against
a real Postgres.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_backfill_shopify_variant_ids_postgres.py

Never point this at prod. CI runs it automatically — the dialect-gate workflow globs
`tests/test_*_postgres.py`.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (
  id TEXT,
  market TEXT DEFAULT 'US',
  tool TEXT DEFAULT '*',
  destination_url TEXT,
  canonical_url TEXT NULL,
  domain TEXT NULL,
  seed_data JSONB DEFAULT '{}'::jsonb,
  status TEXT DEFAULT 'active',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    # Connect/disconnect PER TEST: the suite runs each test on a fresh event loop
    # (asyncio_default_fixture_loop_scope=function), and an asyncpg pool that outlives its
    # loop fails with "attached to a different loop". Same reasoning as
    # tests/test_acp_checkout_sessions_postgres.py.
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # The full Postgres gate shares one throwaway database across modules. Earlier tests
    # leave minimal same-named seed tables; IF NOT EXISTS alone keeps that incompatible
    # shape. Extend only the columns this fixture needs. Dropping/recreating the table
    # instead poisons later modules whose lightweight seed INSERTs omit `id`.
    await database.execute(DDL)
    for name, column_type in (
        ("market", "TEXT DEFAULT 'US'"),
        ("tool", "TEXT DEFAULT '*'"),
        ("destination_url", "TEXT"),
        ("canonical_url", "TEXT"),
        ("domain", "TEXT"),
        ("seed_data", "JSONB DEFAULT '{}'::jsonb"),
        ("status", "TEXT DEFAULT 'active'"),
        ("updated_at", "TIMESTAMPTZ DEFAULT NOW()"),
    ):
        await database.execute(
            f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {name} {column_type}"
        )
    await database.execute("TRUNCATE external_product_seeds")
    yield database
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _insert(
    db,
    seed_id: str,
    seed_data: Any,
    *,
    url: str = "https://brand.com/products/handle",
    domain: str = "brand.com",
    status: str = "active",
    canonical: Optional[str] = None,
    market: str = "US",
) -> None:
    # CAST via ::jsonb from text so a deliberately malformed shape can be stored.
    await db.execute(
        """
        INSERT INTO external_product_seeds
            (id, destination_url, canonical_url, domain, seed_data, status, market)
        VALUES (:id, :url, :canonical, :domain, CAST(:seed_data AS jsonb), :status, :market)
        """,
        {
            "market": market,
            "id": seed_id,
            "url": url,
            "canonical": canonical,
            "domain": domain,
            "seed_data": json.dumps(seed_data) if not isinstance(seed_data, str) else seed_data,
            "status": status,
        },
    )


def _snapshot(*variants: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"snapshot": {"variants": list(variants), **extra}}


async def _select(
    limit: int = 50, domain: Optional[str] = None, after: Optional[str] = None
) -> List[Dict[str, Any]]:
    from scripts.backfill_shopify_variant_ids import select_candidates

    return await select_candidates(limit=limit, domain=domain, after=after)


# ---------------------------------------------------------------------------- selection

async def test_a_malformed_variants_shape_does_not_abort_the_whole_run(_db) -> None:
    """THE P0 THIS FILE EXISTS FOR.

    `jsonb_array_length` raises on a non-array. With the guard as a sibling qual the planner
    was free to run the function first, so ONE row like this raised
    "cannot get array length of a non-array" for the entire cohort and the script produced a
    traceback instead of a candidate list. The CASE wrap makes guard and accessor one
    expression, which the planner cannot separate.
    """
    await _insert(_db, "good", _snapshot({"title": "30ml"}))
    await _insert(_db, "obj", {"snapshot": {"variants": {"not": "an array"}}})
    await _insert(_db, "scalar", {"snapshot": {"variants": 7}})
    await _insert(_db, "str", {"snapshot": {"variants": "nope"}})
    await _insert(_db, "nosnap", {"title": "no snapshot at all"})

    rows = await _select()

    assert [r["id"] for r in rows] == ["good"]


async def test_the_guard_holds_under_a_hostile_planner(_db) -> None:
    """Not a bet on clause order.

    Forcing the planner away from any incidental ordering must not change the outcome — this
    is what distinguishes a real guard from one that only happens to be evaluated late.
    """
    await _insert(_db, "good", _snapshot({"title": "30ml"}))
    await _insert(_db, "obj", {"snapshot": {"variants": {"not": "an array"}}})

    await _db.execute("SET LOCAL enable_seqscan = off")
    await _db.execute("SET LOCAL from_collapse_limit = 1")
    rows = await _select()

    assert [r["id"] for r in rows] == ["good"]


async def test_a_double_encoded_seed_data_string_is_never_selected(_db) -> None:
    """A row stored as a JSON *string* must never be merged into.

    Honest note: on a string, `seed_data->'snapshot'` is NULL, so the array clause already
    excludes it and `jsonb_typeof(seed_data) = 'object'` is defence-in-depth HERE. The guard
    that actually carries weight is the one on the UPDATE — see the next test, which is
    where jsonb_set would otherwise raise mid-sweep.
    """
    await _insert(_db, "encoded", json.dumps(json.dumps({"snapshot": {"variants": [{"t": 1}]}})))
    await _insert(_db, "good", _snapshot({"title": "30ml"}))

    assert [r["id"] for r in await _select()] == ["good"]


async def test_the_write_refuses_a_double_encoded_row_instead_of_raising(_db) -> None:
    """`jsonb_set` on a scalar raises "cannot set path in scalar", which would abort the
    sweep mid-run rather than skip one row.

    Mutation-verified which guard does the work: dropping
    `jsonb_typeof(seed_data->'snapshot') = 'object'` is what turns this refusal into a raise.
    The `jsonb_typeof(seed_data)` check is redundant given it (`->` on a scalar yields NULL)
    and survives mutation — it is intent, not protection, and the comment in the SQL now
    says so rather than implying otherwise."""
    await _insert(_db, "encoded", json.dumps(json.dumps({"snapshot": {"variants": [{"t": 1}]}})))
    stamp = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 'encoded'")

    assert await _stamp(_db, "encoded", [{"shopify_variant_id": "11"}], stamp) is None


async def test_a_fully_stamped_sole_row_is_rechecked_for_cart_proof(_db) -> None:
    """Historical stamps lack sole-storefront proof; partial rows still need recovery."""
    await _insert(_db, "done", _snapshot({"title": "a", "shopify_variant_id": "1"}))
    await _insert(_db, "partial", _snapshot({"title": "a", "shopify_variant_id": "1"}, {"title": "b"}))

    assert [r["id"] for r in await _select()] == ["done", "partial"]


async def test_an_empty_canonical_url_falls_through_to_destination_url(_db) -> None:
    """COALESCE alone kept the empty string and dropped the row; the Python fetch site uses
    `or` and falls through, so the two halves disagreed."""
    await _insert(_db, "blank", _snapshot({"title": "30ml"}), canonical="")

    assert [r["id"] for r in await _select()] == ["blank"]


async def test_only_active_seeds_and_only_product_urls(_db) -> None:
    await _insert(_db, "inactive", _snapshot({"title": "a"}), status="disabled")
    await _insert(_db, "collection", _snapshot({"title": "a"}), url="https://brand.com/collections/all")
    await _insert(_db, "good", _snapshot({"title": "a"}))

    assert [r["id"] for r in await _select()] == ["good"]


async def test_domain_filter_is_a_suffix_match_that_cannot_widen(_db) -> None:
    """`--domain brand.com` must cover www.brand.com but never notbrand.com."""
    await _insert(_db, "bare", _snapshot({"title": "a"}), domain="brand.com")
    await _insert(_db, "www", _snapshot({"title": "a"}), domain="www.brand.com")
    await _insert(_db, "evil", _snapshot({"title": "a"}), domain="notbrand.com")

    assert sorted(r["id"] for r in await _select(domain="brand.com")) == ["bare", "www"]


async def test_like_metacharacters_in_the_domain_cannot_widen_the_cohort(_db) -> None:
    await _insert(_db, "target", _snapshot({"title": "a"}), domain="www.b_and.com")
    await _insert(_db, "other", _snapshot({"title": "a"}), domain="www.brand.com")

    assert [r["id"] for r in await _select(domain="b_and.com")] == ["target"]


# ---------------------------------------------------------------------------- the write

async def _stamp(db, seed_id: str, variants: List[Dict[str, Any]], updated_at: Any,
                 cart_proof: Optional[Dict[str, Any]] = None) -> Optional[str]:
    from scripts.backfill_shopify_variant_ids import (
        STAMP_UPDATE_SQL,
        STOREFRONT_PLATFORM,
        STOREFRONT_PLATFORM_SOURCE,
    )

    return await db.fetch_val(
        STAMP_UPDATE_SQL,
        {
            "id": seed_id,
            "variants": json.dumps(variants),
            "platform": STOREFRONT_PLATFORM,
            "platform_source": STOREFRONT_PLATFORM_SOURCE,
            "cart_proof": json.dumps(cart_proof),
            "variant_proofs": json.dumps({}),
            "updated_at": updated_at,
        },
    )


async def _seed_data(db, seed_id: str) -> Dict[str, Any]:
    raw = await db.fetch_val(
        "SELECT seed_data FROM external_product_seeds WHERE id = :id", {"id": seed_id}
    )
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_a_successful_write_returns_its_id(_db) -> None:
    """`databases.execute()` is `fetchval`, which returns None for a non-RETURNING UPDATE no
    matter how many rows it touched — so every successful write was counted as a conflict
    while the write landed. RETURNING makes None mean only "no row matched"."""
    await _insert(_db, "s1", _snapshot({"title": "30ml"}))
    stamp = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 's1'")

    assert await _stamp(_db, "s1", [{"title": "30ml", "shopify_variant_id": "11"}], stamp) == "s1"
    assert (await _seed_data(_db, "s1"))["snapshot"]["variants"][0]["shopify_variant_id"] == "11"


async def test_a_row_moved_by_the_refresh_job_is_refused_and_counted(_db) -> None:
    """WHAT THE OPTIMISTIC GUARD IS ACTUALLY FOR.

    `_refresh_external_seed_by_id` rewrites this same document and DOES bump `updated_at`.
    A candidate list read minutes earlier (this script walks at 1 req/s) can therefore be
    stale by the time the write lands, and a whole-document jsonb_set would silently revert
    the refresh's price and availability. The guard turns that into a refusal.
    """
    await _insert(_db, "s1", _snapshot({"title": "30ml"}))
    stale = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 's1'")

    # the refresh job lands between our SELECT and our UPDATE
    await _db.execute(
        "UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{price_amount}', '22.4'), "
        "updated_at = NOW() + interval '1 second' WHERE id = 's1'"
    )

    assert await _stamp(_db, "s1", [{"title": "30ml", "shopify_variant_id": "11"}], stale) is None
    after = await _seed_data(_db, "s1")
    assert after["price_amount"] == 22.4, "the refresh's write must survive"
    assert "shopify_variant_id" not in json.dumps(after)


async def test_two_runs_of_THIS_script_are_last_write_wins_by_design(_db) -> None:
    """The guard is deliberately INERT against this script's own re-run, and that is correct.

    This script does not bump `updated_at` (see the no-bump test below), so a second run
    reading the same timestamp still matches. That is safe precisely because two runs derive
    their answer from the same `/products/<handle>.js`: the second write is the first one
    recomputed, not a competing edit. The asymmetry is intentional — the guard exists to lose
    against the REFRESH job, not against itself.
    """
    await _insert(_db, "s1", _snapshot({"title": "30ml"}))
    stamp = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 's1'")

    assert await _stamp(_db, "s1", [{"title": "30ml", "shopify_variant_id": "11"}], stamp) == "s1"
    assert await _stamp(_db, "s1", [{"title": "30ml", "shopify_variant_id": "11"}], stamp) == "s1"
    assert (await _seed_data(_db, "s1"))["snapshot"]["variants"][0]["shopify_variant_id"] == "11"


async def test_the_write_merges_and_never_replaces_the_document(_db) -> None:
    """Everything outside snapshot.variants — title, description, manual_overrides, and the
    rest of snapshot — must survive."""
    original = {
        "title": "Curated Title",
        "description": "curated copy",
        "manual_overrides": {"description": True},
        "snapshot": {"variants": [{"title": "30ml"}], "extracted_at": "2026-08-01T00:00:00Z"},
    }
    await _insert(_db, "s1", original)
    stamp = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 's1'")

    await _stamp(_db, "s1", [{"title": "30ml", "shopify_variant_id": "11"}], stamp)
    after = await _seed_data(_db, "s1")

    assert after["title"] == "Curated Title"
    assert after["description"] == "curated copy"
    assert after["manual_overrides"] == {"description": True}
    assert after["snapshot"]["extracted_at"] == "2026-08-01T00:00:00Z"


async def test_the_write_stamps_the_platform_evidence_the_consumer_reads(_db) -> None:
    """The gateway's `storefront_is_shopify` reads these exact keys; without them the
    recovered ids are inert. Producer and consumer are pinned together here."""
    from services.shopify_variant_identity import sole_stamped_variant_id, storefront_is_shopify

    await _insert(_db, "s1", _snapshot({"title": "30ml"}))
    stamp = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 's1'")
    await _stamp(_db, "s1", [{"title": "30ml", "shopify_variant_id": "41234567890123"}], stamp)

    after = await _seed_data(_db, "s1")
    assert after["snapshot"]["storefront_platform"] == "shopify"
    assert after["snapshot"]["storefront_platform_source"] == "products_js_v1"
    # and the consumer accepts it
    assert storefront_is_shopify(after) is True
    assert sole_stamped_variant_id(after) == "41234567890123"


async def test_the_write_targets_snapshot_and_leaves_top_level_alone(_db) -> None:
    """Writing to top-level `variants` would SHADOW the snapshot array for every serving
    reader that prefers top-level."""
    await _insert(_db, "s1", {"variants": [{"title": "top-level"}], **_snapshot({"title": "30ml"})})
    stamp = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 's1'")

    await _stamp(_db, "s1", [{"title": "30ml", "shopify_variant_id": "11"}], stamp)
    after = await _seed_data(_db, "s1")

    assert after["variants"] == [{"title": "top-level"}], "top-level must be untouched"
    assert after["snapshot"]["variants"][0]["shopify_variant_id"] == "11"


async def test_the_write_does_not_bump_updated_at(_db) -> None:
    """`get_last_extracted_at` falls back to updated_at, feeding the 7-day stale_snapshot
    BLOCKER — a variants-only .js fetch is not an extraction event, and bumping it would
    un-block seeds whose price and availability were never re-read."""
    await _insert(_db, "s1", _snapshot({"title": "30ml"}))
    before = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 's1'")

    await _stamp(_db, "s1", [{"title": "30ml", "shopify_variant_id": "11"}], before)
    after = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 's1'")

    assert after == before


async def test_the_write_refuses_a_row_whose_snapshot_is_not_an_object(_db) -> None:
    """`jsonb_set` raises "path element is not an integer" when snapshot is an array; the
    guard turns that into a no-op rather than an exception mid-sweep."""
    await _insert(_db, "arr", {"snapshot": ["not", "an", "object"]})
    stamp = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 'arr'")

    assert await _stamp(_db, "arr", [{"shopify_variant_id": "11"}], stamp) is None


async def test_the_write_touches_exactly_one_row(_db) -> None:
    """`WHERE id = :id` deleted would stamp one row's variants onto every row sharing its
    updated_at — a mutant that survived the previous, text-assertion-only test."""
    await _insert(_db, "a", _snapshot({"title": "30ml"}))
    await _insert(_db, "b", _snapshot({"title": "30ml"}))
    stamp_a = await _db.fetch_val("SELECT updated_at FROM external_product_seeds WHERE id = 'a'")

    await _stamp(_db, "a", [{"title": "30ml", "shopify_variant_id": "11"}], stamp_a)

    assert (await _seed_data(_db, "b"))["snapshot"]["variants"] == [{"title": "30ml"}]


# ---------------------------------------------------------------------------- end to end

async def test_run_end_to_end_against_postgres_with_a_faked_storefront(_db) -> None:
    """The whole loop: select -> fetch -> stamp -> write -> report, with only the network
    faked. Asserts the report's counters match what actually landed in the database."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert(_db, "s1", _snapshot({"title": "30ml"}),
                  url="https://brand.com/products/serum", domain="brand.com")

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json() -> Dict[str, Any]:
            return {"variants": [{"id": 41234567890123, "title": "30ml", "options": ["30ml"],
                                  "price": 2240, "available": True}]}

    class _Client:
        calls: List[str] = []

        async def get(self, url, **kwargs):
            _Client.calls.append(url)
            return _Resp()

    summary = await run(limit=10, domain=None, apply=True, client=_Client())

    # The fake sends no currency cookie, so the run asks the product's .json for its price (and
    # gets the same .js body back, which is not a product JSON: no price is written).
    assert _Client.calls == ["https://brand.com/products/serum.js?country=US",
                             "https://brand.com/products/serum.json?country=US"]
    assert summary["proof_currency"] == {"json_malformed": 1}
    assert summary["rows_with_new_ids"] == 1
    assert summary["variant_ids_stamped"] == 1
    assert summary["write_conflicts"] == 0
    after = await _seed_data(_db, "s1")
    assert after["snapshot"]["variants"][0]["shopify_variant_id"] == "41234567890123"
    assert after["snapshot"]["storefront_platform"] == "shopify"
    assert after["snapshot"]["shopify_cart_proof"]["variant_id"] == "41234567890123"
    assert after["snapshot"]["shopify_cart_proof"]["live_variant_count"] == 1


async def test_a_multivariant_storefront_revokes_old_sole_cart_proof(_db) -> None:
    """A prior sole proof cannot survive a fresh .js response with two live choices."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert(_db, "s1", _snapshot(
        {"title": "30ml", "shopify_variant_id": "11"},
        shopify_cart_proof={"source": "products_js_v1", "variant_id": "11"},
    ))

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"variants": [
                {"id": 11, "title": "30ml", "options": ["30ml"]},
                {"id": 22, "title": "50ml", "options": ["50ml"]},
            ]}

    class _Client:
        async def get(self, url, **kwargs):
            return _Resp()

    await run(limit=10, domain=None, apply=True, client=_Client())
    assert (await _seed_data(_db, "s1"))["snapshot"]["shopify_cart_proof"] is None


async def test_a_dry_run_writes_nothing_but_reports_what_it_would_do(_db) -> None:
    from scripts.backfill_shopify_variant_ids import run

    await _insert(_db, "s1", _snapshot({"title": "30ml"}), url="https://brand.com/products/serum")

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"variants": [{"id": 11, "title": "30ml", "options": ["30ml"], "available": True}]}

    class _Client:
        async def get(self, url, **kwargs):
            return _Resp()

    summary = await run(limit=10, domain=None, apply=False, client=_Client())

    assert summary["mode"] == "dry_run"
    assert summary["variant_ids_stamped"] == 1
    assert "shopify_variant_id" not in json.dumps(await _seed_data(_db, "s1"))


async def test_a_sustained_block_aborts_instead_of_deepening_it(_db) -> None:
    """A run that keeps going while blocked collects nothing and worsens the block. 403 and
    a 200 challenge page count too — the 429 observed once is not the only shape."""
    from scripts.backfill_shopify_variant_ids import CONSECUTIVE_BLOCK_ABORT, run

    for i in range(CONSECUTIVE_BLOCK_ABORT + 3):
        await _insert(_db, f"s{i:02d}", _snapshot({"title": "30ml"}),
                      url=f"https://brand.com/products/p{i}", domain="brand.com")

    class _Resp:
        status_code = 403
        headers = {"content-type": "text/html"}

    class _Client:
        calls = 0

        async def get(self, url, **kwargs):
            _Client.calls += 1
            return _Resp()

    import scripts.backfill_shopify_variant_ids as mod

    mod.GLOBAL_MIN_INTERVAL_S = 0.0
    mod.PER_DOMAIN_MIN_GAP_S = 0.0
    summary = await run(limit=50, domain=None, apply=True, client=_Client())

    assert summary["aborted_on_block"] is True
    assert _Client.calls == CONSECUTIVE_BLOCK_ABORT, "must stop AT the threshold, not after"


# ------------------------------------------------------- round-5 review: the sweep must progress

async def test_a_sweep_pages_past_rows_it_can_never_stamp(_db) -> None:
    """THE WEDGE. A dead handle or an unmatchable label stays ELIGIBLE forever and sorts
    first, so `ORDER BY id LIMIT n` re-fetched the identical unproductive prefix on every run
    and never reached row n+1. Measured before the fix: four consecutive runs, same three dead
    rows, zero stamped. The cursor is what turns eligibility into progress."""
    for i in range(5):
        await _insert(_db, f"s{i}", _snapshot({"title": "30ml"}),
                      url=f"https://brand.com/products/p{i}")

    first = await _select(limit=2)
    assert [r["id"] for r in first] == ["s0", "s1"]

    second = await _select(limit=2, after=first[-1]["id"])
    assert [r["id"] for r in second] == ["s2", "s3"], "the run must move on, not re-walk"


async def test_the_run_reports_a_resume_point(_db) -> None:
    from scripts.backfill_shopify_variant_ids import run

    for i in range(3):
        await _insert(_db, f"s{i}", _snapshot({"title": "30ml"}),
                      url=f"https://brand.com/products/p{i}")

    class _Dead:
        status_code = 404
        headers = {"content-type": "text/html"}

    class _Client:
        async def get(self, url, **kwargs):
            return _Dead()

    summary = await run(limit=2, domain=None, apply=False, client=_Client())

    assert summary["next_cursor"] == "s1", "a run of pure failures must still advance"
    assert summary["fetch_outcomes"]["dead_handle"] == 2


async def test_junk_in_shopify_variant_id_does_not_retire_a_row(_db) -> None:
    """A live writer lands unvalidated variant keys here. Junk marked the row covered and
    retired it from the backfill permanently — unrepairable, and useless to the consumer,
    which requires a numeric id."""
    await _insert(_db, "junk", _snapshot({"title": "a", "shopify_variant_id": True}))
    await _insert(_db, "obj", _snapshot({"title": "a", "shopify_variant_id": {"a": 1}}))
    await _insert(_db, "real", _snapshot({"title": "a", "shopify_variant_id": "41234567890123"}))

    assert sorted(r["id"] for r in await _select()) == ["junk", "obj", "real"]


async def test_a_brands_dead_handles_do_not_abort_the_whole_sweep(_db) -> None:
    """`not_json` conflated a Cloudflare challenge with a THEMED SOFT-404 — the same bytes,
    opposite facts. Wired to the global abort, one brand's rot bricked the backfill: every
    later run selected the same rows, aborted, exited 1, stamped nothing."""
    from scripts.backfill_shopify_variant_ids import CONSECUTIVE_BLOCK_ABORT, run
    import scripts.backfill_shopify_variant_ids as mod

    for i in range(CONSECUTIVE_BLOCK_ABORT + 2):
        await _insert(_db, f"s{i:02d}", _snapshot({"title": "30ml"}),
                      url=f"https://brand.com/products/p{i}")

    class _SoftFourOhFour:
        status_code = 200
        headers = {"content-type": "text/html"}

    class _Client:
        async def get(self, url, **kwargs):
            return _SoftFourOhFour()

    prev = (mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S)
    mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S = 0.0, 0.0
    try:
        summary = await run(limit=50, domain=None, apply=False, client=_Client())
    finally:
        mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S = prev

    assert summary["aborted_on_block"] is False, "soft-404s are the seed's rot, not our block"
    assert summary["fetch_outcomes"]["not_json"] == CONSECUTIVE_BLOCK_ABORT + 2


async def test_403s_interleaved_with_connection_resets_still_abort(_db) -> None:
    """What an IP-level block actually looks like. Treating a reset as "not a block" reset the
    counter on every other request — 40 requests into a live block without aborting."""
    from scripts.backfill_shopify_variant_ids import CONSECUTIVE_BLOCK_ABORT, run
    import scripts.backfill_shopify_variant_ids as mod

    for i in range(40):
        await _insert(_db, f"s{i:02d}", _snapshot({"title": "30ml"}),
                      url=f"https://brand.com/products/p{i}")

    class _Forbidden:
        status_code = 403
        headers = {"content-type": "text/html"}

    class _Client:
        n = 0

        async def get(self, url, **kwargs):
            _Client.n += 1
            if _Client.n % 2 == 0:
                raise ConnectionResetError("peer reset")
            return _Forbidden()

    prev = (mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S)
    mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S = 0.0, 0.0
    try:
        summary = await run(limit=50, domain=None, apply=False, client=_Client())
    finally:
        mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S = prev

    assert summary["aborted_on_block"] is True
    assert _Client.n == CONSECUTIVE_BLOCK_ABORT


async def test_domain_bounds_the_hosts_actually_contacted(_db) -> None:
    """`--domain` filtered the domain COLUMN while the fetch host came from canonical_url.
    The rollout plan and the shared-NAT blast-radius argument both assume it bounds which
    hosts are contacted."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert(_db, "mismatch", _snapshot({"title": "a"}), domain="brand.com",
                  url="https://other-store.example/products/x")

    class _Client:
        calls: List[str] = []

        async def get(self, url, **kwargs):
            _Client.calls.append(url)
            raise AssertionError("must not be fetched")

    summary = await run(limit=5, domain="brand.com", apply=False, client=_Client())

    assert _Client.calls == []
    assert summary["fetch_outcomes"]["host_outside_domain_filter"] == 1


async def test_a_block_alternating_403_with_a_challenge_page_still_aborts(_db) -> None:
    """Round-6: reclassifying `not_json` out of BLOCK_OUTCOMES re-opened the hole the
    `error:*` rule had just closed — an ambiguous challenge page RESET the streak, so a
    403/challenge alternating block ran 22 requests deep without aborting. `not_json` is now
    neutral: it neither aborts nor resets."""
    from scripts.backfill_shopify_variant_ids import CONSECUTIVE_BLOCK_ABORT, run
    import scripts.backfill_shopify_variant_ids as mod

    for i in range(40):
        await _insert(_db, f"s{i:02d}", _snapshot({"title": "30ml"}),
                      url=f"https://brand.com/products/p{i}", domain="brand.com")

    class _Forbidden:
        status_code = 403
        headers = {"content-type": "text/html"}

    class _Challenge:
        status_code = 200
        headers = {"content-type": "text/html"}

    class _Client:
        n = 0

        async def get(self, url, **kwargs):
            _Client.n += 1
            return _Challenge() if _Client.n % 2 == 0 else _Forbidden()

    prev = (mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S)
    mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S = 0.0, 0.0
    try:
        summary = await run(limit=50, domain=None, apply=False, client=_Client())
    finally:
        mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S = prev

    assert summary["aborted_on_block"] is True
    assert _Client.n < 20, f"must not run {_Client.n} requests into a live block"
    # and the ambiguous outcome is visible per-domain, as the comment claims
    assert summary["most_blocked_domains"].get("brand.com", 0) > 0


async def test_an_aborted_run_reports_no_resume_point(_db) -> None:
    """Round-6 F5: without `and not aborted`, a resumed sweep permanently skips every row
    fetched after the abort — they were never actually examined."""
    from scripts.backfill_shopify_variant_ids import CONSECUTIVE_BLOCK_ABORT, run
    import scripts.backfill_shopify_variant_ids as mod

    for i in range(CONSECUTIVE_BLOCK_ABORT + 5):
        await _insert(_db, f"s{i:02d}", _snapshot({"title": "30ml"}),
                      url=f"https://brand.com/products/p{i}")

    class _Forbidden:
        status_code = 403
        headers = {"content-type": "text/html"}

    class _Client:
        async def get(self, url, **kwargs):
            return _Forbidden()

    prev = (mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S)
    mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S = 0.0, 0.0
    try:
        summary = await run(limit=50, domain=None, apply=False, client=_Client())
    finally:
        mod.GLOBAL_MIN_INTERVAL_S, mod.PER_DOMAIN_MIN_GAP_S = prev

    assert summary["aborted_on_block"] is True
    assert summary["next_cursor"] is None, "an aborted run must not advance the cursor"


# ---------------------------------------------------------------------------- named-variant proof
#
# Option 1 (2026-09-29): a multi-variant product whose seed NAMES one variant gets a
# `scope: named_variant` proof from the same fetch. Driven with the REAL shapes: the live
# judydoll products.js (8 shades) and the live seed row (tests/fixtures/judydoll_*).

from pathlib import Path  # noqa: E402

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_JUDY_JS = json.loads((_FIXTURES / "judydoll_silky_matte_lip_ink_products_js_2026_09_29.json").read_text())
_JUDY_ROW = json.loads((_FIXTURES / "judydoll_silky_matte_lip_ink_seed_2026_09_29.json").read_text())
_JUDY_VARIANT = "49819267301653"


_JUDY_JSON = json.loads((_FIXTURES / "judydoll_silky_matte_lip_ink_product_json_2026_10_08.json").read_text())


def _judy_client(payload: Dict[str, Any], *, set_cookies=("cart_currency=USD; path=/; SameSite=Lax",),
                 json_payload: Optional[Dict[str, Any]] = _JUDY_JSON):
    """The storefront: `.js` with `set_cookies` on its response; `/products/<handle>.json` (the
    cookieless-store price fallback) with `json_payload`, or a 404 when None. judydoll itself
    really sends NO cart_currency cookie (measured 2026-10-08); USD is the default here so the
    cases above exercise the cookie path."""
    import httpx

    class _Resp:
        def __init__(self, body: Any, headers: List[Any], status_code: int = 200) -> None:
            self._body, self.headers, self.status_code = body, httpx.Headers(headers), status_code

        def json(self) -> Any:
            return self._body

    class _Client:
        calls: List[str] = []

        async def get(self, url, **kwargs):
            _Client.calls.append(url)
            if url.split("?", 1)[0].endswith(".json"):
                if json_payload is None:
                    return _Resp(None, [("content-type", "text/html")], status_code=404)
                return _Resp(json_payload, [("content-type", "application/json; charset=utf-8")])
            # what judydoll really sends, plus the presentment-currency cookie(s) of this response
            return _Resp(payload, [("content-type", "text/javascript; charset=utf-8")]
                         + [("set-cookie", value) for value in set_cookies])

    return _Client()


async def _insert_judy(db, seed_data: Optional[Dict[str, Any]] = None, *, canonical: Optional[str] = None,
                       market: str = "US") -> None:
    await _insert(db, _JUDY_ROW["id"], seed_data if seed_data is not None else _JUDY_ROW["seed_data"],
                  url=_JUDY_ROW["destination_url"], domain=_JUDY_ROW["domain"],
                  canonical=canonical if canonical is not None else _JUDY_ROW["canonical_url"], market=market)


def _judy_js(**overrides: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.loads(json.dumps(_JUDY_JS))
    for variant in payload["variants"]:
        variant.update(overrides.get(str(variant["id"]), {}))
    return payload


@pytest.mark.parametrize("canonical", [None, "prod"], ids=["staging_canonical", "prod_canonical"])
async def test_the_live_judydoll_seed_gets_a_named_variant_proof_end_to_end(_db, canonical) -> None:
    from scripts.backfill_shopify_variant_ids import run
    from services.shopify_variant_identity import verified_cart_variant_id

    await _insert_judy(_db, canonical=_JUDY_ROW["destination_url"] if canonical else None)
    client = _judy_client(_JUDY_JS)
    summary = await run(limit=10, domain="judydoll.com", apply=True, client=client)

    assert client.calls == ["https://judydoll.com/products/silky-matte-lip-ink.js?country=US"]
    assert summary["cart_proofs"] == {"named_variant": 1}
    assert summary["write_conflicts"] == 0 and summary["match_reasons"] == {"label_match": 1}
    after = await _seed_data(_db, _JUDY_ROW["id"])
    proof = after["snapshot"]["shopify_cart_proof"]
    assert after["snapshot"]["variants"][0]["shopify_variant_id"] == _JUDY_VARIANT
    assert {k: proof[k] for k in ("source", "scope", "variant_id", "available", "live_variant_count",
                                  "price_minor", "currency", "product_js_url")} == {
        "source": "products_js_v1", "scope": "named_variant", "variant_id": _JUDY_VARIANT,
        "available": True, "live_variant_count": 8, "price_minor": 1399, "currency": "USD",
        "product_js_url": "https://judydoll.com/products/silky-matte-lip-ink.js"}
    assert summary["proof_currency"] == {"cookie:USD": 1}
    # what was WRITTEN is what the Reap route accepts
    row = await _db.fetch_one("SELECT canonical_url, destination_url FROM external_product_seeds WHERE id = :id",
                              {"id": _JUDY_ROW["id"]})
    proven = verified_cart_variant_id(
        after, product_urls=[row["canonical_url"] or row["destination_url"]],
        shop_domain="judydoll.com", catalog_variant_id=_JUDY_VARIANT)
    assert proven == (_JUDY_VARIANT, "named_variant", "07 BURGUNDY INK")


@pytest.mark.parametrize("change", ["unavailable", "delisted"])
async def test_a_named_proof_is_revoked_when_its_variant_goes_away(_db, change) -> None:
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db)
    await run(limit=10, domain="judydoll.com", apply=True, client=_judy_client(_JUDY_JS))
    assert (await _seed_data(_db, _JUDY_ROW["id"]))["snapshot"]["shopify_cart_proof"]["scope"] == "named_variant"

    payload = _judy_js(**{_JUDY_VARIANT: {"available": False}})
    if change == "delisted":
        payload["variants"] = [v for v in _JUDY_JS["variants"] if str(v["id"]) != _JUDY_VARIANT]
    # the row with a proof is RE-SELECTED (a named seed must stay in the backfill's cohort)
    assert [r["id"] for r in await _select(domain="judydoll.com")] == [_JUDY_ROW["id"]]
    summary = await run(limit=10, domain="judydoll.com", apply=True, client=_judy_client(payload))
    assert summary["cart_proofs"] == {}
    assert (await _seed_data(_db, _JUDY_ROW["id"]))["snapshot"]["shopify_cart_proof"] is None


async def test_a_valid_sole_proof_is_never_downgraded_to_a_named_one(_db) -> None:
    """A one-variant storefront keeps writing the UNSCOPED proof even when the seed's URL also
    names that variant -- the sole proof is tried first, from the same fetch."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db, canonical=_JUDY_ROW["destination_url"])
    sole = {"variants": [v for v in _JUDY_JS["variants"] if str(v["id"]) == _JUDY_VARIANT]}
    for _ in range(2):  # first write, then the re-check of a row that already holds a sole proof
        summary = await run(limit=10, domain="judydoll.com", apply=True, client=_judy_client(sole))
        assert summary["cart_proofs"] == {"sole_variant": 1}
        proof = (await _seed_data(_db, _JUDY_ROW["id"]))["snapshot"]["shopify_cart_proof"]
        assert "scope" not in proof and proof["live_variant_count"] == 1
        assert proof["variant_id"] == _JUDY_VARIANT


async def test_named_variant_seeds_are_selected_in_every_state(_db) -> None:
    """Selection stays in step: unstamped (never fetched), stamped single entry without a proof
    key (prod's 1,933 rows stamped before proofs existed), a named proof, and a revoked (null)
    proof are ALL candidates. A 2+ entry snapshot names nothing -- it is selected only while it
    has unstamped entries, exactly as before."""
    entry = _JUDY_ROW["seed_data"]["snapshot"]["variants"][0]
    stamped = dict(entry, shopify_variant_id=_JUDY_VARIANT)
    named_proof = {"source": "products_js_v1", "scope": "named_variant", "variant_id": _JUDY_VARIANT}
    await _insert(_db, "a_unstamped", _snapshot(entry))
    await _insert(_db, "b_stamped_no_proof", _snapshot(stamped))
    await _insert(_db, "c_named_proof", _snapshot(stamped, shopify_cart_proof=named_proof))
    await _insert(_db, "d_revoked", _snapshot(stamped, shopify_cart_proof=None))
    await _insert(_db, "e_two_stamped", _snapshot(stamped, dict(stamped, shopify_variant_id="1")))

    assert [r["id"] for r in await _select()] == [
        "a_unstamped", "b_stamped_no_proof", "c_named_proof", "d_revoked", "e_two_stamped"]


async def test_a_dry_run_reports_the_named_proof_without_writing_it(_db) -> None:
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db)
    summary = await run(limit=10, domain="judydoll.com", apply=False, client=_judy_client(_JUDY_JS))
    assert summary["mode"] == "dry_run" and summary["cart_proofs"] == {"named_variant": 1}
    assert "shopify_cart_proof" not in (await _seed_data(_db, _JUDY_ROW["id"]))["snapshot"]


async def test_seed_id_targets_exactly_the_named_seeds_under_the_same_eligibility(_db) -> None:
    """F3 (#2459 review): `--seed-id` restricts selection to those ids and to nothing else -- it
    never widens past the eligibility rules, and it composes with --domain / --after / --limit."""
    from scripts.backfill_shopify_variant_ids import run

    entry = _JUDY_ROW["seed_data"]["snapshot"]["variants"][0]
    await _insert(_db, "epsv_0first", _snapshot(entry), url=_JUDY_ROW["canonical_url"], domain="judydoll.com")
    await _insert_judy(_db)
    await _insert(_db, "epsv_zlast", _snapshot(entry), url=_JUDY_ROW["canonical_url"], domain="judydoll.com")
    await _insert(_db, "epsv_inactive", _snapshot(entry), url=_JUDY_ROW["canonical_url"],
                  domain="judydoll.com", status="inactive")
    await _insert(_db, "epsv_other_shop", _snapshot(entry), url="https://brand.com/products/x",
                  domain="brand.com")
    target = _JUDY_ROW["id"]

    assert [r["id"] for r in await _select(domain="judydoll.com", limit=1)] == ["epsv_0first"], \
        "without targeting, ORDER BY id LIMIT 1 walks the wrong seed"
    from scripts.backfill_shopify_variant_ids import select_candidates
    pick = lambda **kw: select_candidates(**{"limit": 50, "domain": None, **kw})  # noqa: E731
    assert [r["id"] for r in await pick(seed_ids=[target], limit=1)] == [target]
    assert [r["id"] for r in await pick(seed_ids=[target, "epsv_zlast", target])] == [target, "epsv_zlast"]
    assert [r["id"] for r in await pick(seed_ids=["epsv_inactive"])] == [], "eligibility still applies"
    assert [r["id"] for r in await pick(seed_ids=[target], domain="brand.com")] == []
    assert [r["id"] for r in await pick(seed_ids=["epsv_other_shop", target], domain="judydoll.com")] == [target]
    assert [r["id"] for r in await pick(seed_ids=[target], after=target)] == []
    assert [r["id"] for r in await pick(seed_ids=["x' OR '1'='1"])] == [], "bound, never interpolated"

    client = _judy_client(_JUDY_JS)
    summary = await run(limit=1, domain="judydoll.com", apply=False, client=client, seed_ids=[target])
    assert client.calls == ["https://judydoll.com/products/silky-matte-lip-ink.js?country=US"]
    assert summary["candidates"] == 1 and summary["next_cursor"] == target
    assert summary["cart_proofs"] == {"named_variant": 1}


def test_seed_id_is_a_repeatable_cli_flag(monkeypatch) -> None:
    import scripts.backfill_shopify_variant_ids as backfill

    seen: Dict[str, Any] = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return {"aborted_on_block": False}

    class _Db:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    monkeypatch.setattr(backfill, "run", fake_run)
    monkeypatch.setattr(backfill, "database", _Db())
    monkeypatch.setattr("sys.argv", ["backfill", "--seed-id", "epsv_a", "--seed-id", "epsv_b",
                                     "--domain", "judydoll.com", "--limit", "2"])
    assert backfill.main() == 0
    assert seen["seed_ids"] == ["epsv_a", "epsv_b"] and seen["domain"] == "judydoll.com"
    monkeypatch.setattr("sys.argv", ["backfill"])
    backfill.main()
    assert seen["seed_ids"] is None


async def test_fully_stamped_multivariant_row_needs_selector_proof_refresh(_db):
    await _insert(_db, "selectable", _snapshot({"shopify_variant_id":"11"},{"shopify_variant_id":"22"}))
    assert [r["id"] for r in await _select()] == ["selectable"]


# ---------------------------------------------------------------------------- proof currency (PR 3)
#
# A mirror proof CORROBORATES a changed Reap price only when it records the currency its price
# was read in (services/reap_price_corroboration.py, THE CURRENCY DECISION). The writer reads it
# by THE CURRENCY RULE (services/shopify_presentment.py): `?country=<market>` on the request, the
# `cart_currency` Set-Cookie of the same response, and only the market's own currency counts.
# Every case runs the REAL writer into Postgres and the REAL reader over what it wrote.


def _mirror_price(seed_data: Dict[str, Any], *, currency: str = "USD") -> Optional[int]:
    from datetime import datetime, timedelta, timezone

    from services import reap_price_corroboration as corroboration

    return corroboration.mirror_unit_price(
        seed_data, variant_id=_JUDY_VARIANT, product_urls=[_JUDY_ROW["canonical_url"]],
        shop_domain="judydoll.com", currency=currency, now=datetime.now(timezone.utc),
        max_age=timedelta(hours=72))


async def test_the_written_proof_corroborates_the_live_price(_db) -> None:
    """The writer's own proof, read back from Postgres, is what the purchase lane accepts: 1399
    in USD for the US market. Not a hand-added currency key."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db)
    summary = await run(limit=10, domain="judydoll.com", apply=True, client=_judy_client(_JUDY_JS))
    assert summary["proof_currency"] == {"cookie:USD": 1}
    after = await _seed_data(_db, _JUDY_ROW["id"])
    assert _mirror_price(after) == 1399
    assert _mirror_price(after, currency="SGD") is None


@pytest.mark.parametrize("set_cookies, problem", [
    (("cart_currency=USD", "cart_currency=GBP"), "cookie_conflict"),
    (("cart_currency=usd",), "cookie_malformed"),
    (("cart_currency=SGD",), "currency_not_market"),
], ids=["conflict", "malformed", "not_market"])
async def test_an_unverified_currency_writes_no_price(_db, set_cookies, problem) -> None:
    """A cookie that is present but not the market's one verified currency -> the proof is still
    written (the cart identity does not depend on price) but carries NO price and NO currency, so
    it never corroborates. No .json fallback: the store did name a currency, just not this one."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db)
    client = _judy_client(_JUDY_JS, set_cookies=set_cookies)
    summary = await run(limit=10, domain="judydoll.com", apply=True, client=client)
    assert summary["cart_proofs"] == {"named_variant": 1}
    assert summary["proof_currency"] == {problem: 1} and summary["json_price_fetches"] == {}
    assert len(client.calls) == 1
    after = await _seed_data(_db, _JUDY_ROW["id"])
    proof = after["snapshot"]["shopify_cart_proof"]
    assert (proof["price_minor"], proof["currency"], proof["available"]) == (None, None, True)
    assert _mirror_price(after) is None


@pytest.mark.parametrize("set_cookies", [(), ("localization=US; path=/",)], ids=["no_cookie", "other_cookie_only"])
async def test_a_cookieless_store_is_priced_from_its_product_json(_db, set_cookies) -> None:
    """judydoll's real answer: no cart_currency cookie. ONE extra request to the product's .json
    (same market) names each variant's price_currency in the same response -> the proof carries
    1399 USD from `products_json_v1`, and it corroborates."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db)
    client = _judy_client(_JUDY_JS, set_cookies=set_cookies)
    summary = await run(limit=10, domain="judydoll.com", apply=True, client=client)
    assert client.calls == ["https://judydoll.com/products/silky-matte-lip-ink.js?country=US",
                            "https://judydoll.com/products/silky-matte-lip-ink.json?country=US"]
    assert summary["proof_currency"] == {"json:USD": 1} and summary["json_price_fetches"] == {"ok": 1}
    after = await _seed_data(_db, _JUDY_ROW["id"])
    proof = after["snapshot"]["shopify_cart_proof"]
    assert (proof["price_minor"], proof["currency"], proof["price_source"]) == (1399, "USD", "products_json_v1")
    assert proof["product_js_url"] == "https://judydoll.com/products/silky-matte-lip-ink.js"
    assert _mirror_price(after) == 1399


def _json_with(**over: Any) -> Dict[str, Any]:
    body = json.loads(json.dumps(_JUDY_JSON))
    variant = over.pop("variant", None)
    body["product"].update(over)
    if variant:
        for entry in body["product"]["variants"]:
            if str(entry["id"]) == _JUDY_VARIANT:
                entry.update(variant)
    return body


@pytest.mark.parametrize("json_payload, evidence", [
    (None, "json_dead_handle"),
    (_json_with(id=1), "json_other_product"),
    (_json_with(handle="another-product"), "json_other_product"),
    ({"products": []}, "json_malformed"),
    (_json_with(variant={"price_currency": "SGD"}), "json_variant_unpriced"),  # others priced, not this one
], ids=["404", "other_id", "other_handle", "not_a_product", "variant_in_another_currency"])
async def test_the_json_fallback_prices_only_the_same_product_in_the_markets_currency(_db, json_payload, evidence) -> None:
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db)
    summary = await run(limit=10, domain="judydoll.com", apply=True,
                        client=_judy_client(_JUDY_JS, set_cookies=(), json_payload=json_payload))
    assert summary["proof_currency"] == {evidence: 1}
    after = await _seed_data(_db, _JUDY_ROW["id"])
    proof = after["snapshot"]["shopify_cart_proof"]
    assert (proof["price_minor"], proof["currency"], proof["price_source"]) == (None, None, None)
    assert _mirror_price(after) is None


async def test_no_fallback_request_when_no_proof_could_corroborate(_db) -> None:
    """A sold-out named variant writes no proof, so the cookieless store is not asked again."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db)
    client = _judy_client(_judy_js(**{_JUDY_VARIANT: {"available": False}}), set_cookies=())
    summary = await run(limit=10, domain="judydoll.com", apply=True, client=client)
    assert len(client.calls) == 1 and summary["json_price_fetches"] == {}


async def test_the_request_asks_the_seeds_own_market(_db) -> None:
    """An SG seed asks `?country=SG`, and an SGD answer is that market's currency."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db, market="sg")
    client = _judy_client(_JUDY_JS, set_cookies=("cart_currency=SGD; path=/",))
    summary = await run(limit=10, domain="judydoll.com", apply=True, client=client)
    assert client.calls == ["https://judydoll.com/products/silky-matte-lip-ink.js?country=SG"]
    assert summary["proof_currency"] == {"cookie:SGD": 1}
    after = await _seed_data(_db, _JUDY_ROW["id"])
    assert after["snapshot"]["shopify_cart_proof"]["product_js_url"] == \
        "https://judydoll.com/products/silky-matte-lip-ink.js"  # the fetch rule's URL, no query
    assert _mirror_price(after, currency="SGD") == 1399 and _mirror_price(after) is None


async def test_a_market_the_lane_does_not_price_asks_no_country_and_writes_no_price(_db) -> None:
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db, market="DE")
    client = _judy_client(_JUDY_JS, set_cookies=("cart_currency=EUR",))
    summary = await run(limit=10, domain="judydoll.com", apply=True, client=client)
    assert client.calls == ["https://judydoll.com/products/silky-matte-lip-ink.js"]
    assert summary["proof_currency"] == {"market_unknown": 1}
    proof = (await _seed_data(_db, _JUDY_ROW["id"]))["snapshot"]["shopify_cart_proof"]
    assert (proof["price_minor"], proof["currency"]) == (None, None)


async def test_a_currency_verified_refresh_replaces_an_old_uncurrencied_price(_db) -> None:
    """A proof from before this change (x100 `price_minor`, no currency) is overwritten whole by
    the next fetch: the write replaces the proof object, it never merges into it."""
    from scripts.backfill_shopify_variant_ids import run

    await _insert_judy(_db)
    await run(limit=10, domain="judydoll.com", apply=True,
              client=_judy_client(_JUDY_JS, set_cookies=(), json_payload=None))
    await _db.execute(
        "UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, "
        "'{snapshot,shopify_cart_proof,price_minor}', '1399'::jsonb) WHERE id = :id", {"id": _JUDY_ROW["id"]})
    assert _mirror_price(await _seed_data(_db, _JUDY_ROW["id"])) is None  # no currency, no trust
    await run(limit=10, domain="judydoll.com", apply=True, client=_judy_client(_judy_js(**{_JUDY_VARIANT: {"price": 1299}})))
    after = await _seed_data(_db, _JUDY_ROW["id"])
    assert (after["snapshot"]["shopify_cart_proof"]["price_minor"],
            after["snapshot"]["shopify_cart_proof"]["currency"]) == (1299, "USD")
    assert _mirror_price(after) == 1299
