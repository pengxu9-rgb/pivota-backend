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
  id TEXT PRIMARY KEY,
  market TEXT NOT NULL DEFAULT 'US',
  tool TEXT NOT NULL DEFAULT '*',
  destination_url TEXT NOT NULL,
  canonical_url TEXT NULL,
  domain TEXT NULL,
  seed_data JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    await database.execute(DDL)
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
) -> None:
    # CAST via ::jsonb from text so a deliberately malformed shape can be stored.
    await db.execute(
        """
        INSERT INTO external_product_seeds
            (id, destination_url, canonical_url, domain, seed_data, status)
        VALUES (:id, :url, :canonical, :domain, CAST(:seed_data AS jsonb), :status)
        """,
        {
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


async def _select(limit: int = 50, domain: Optional[str] = None) -> List[Dict[str, Any]]:
    from scripts.backfill_shopify_variant_ids import select_candidates

    return await select_candidates(limit=limit, domain=domain)


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


async def test_a_fully_stamped_row_retires_but_a_partial_one_stays_eligible(_db) -> None:
    """Retiring a partially-covered row would permanently strand its unstamped siblings."""
    await _insert(_db, "done", _snapshot({"title": "a", "shopify_variant_id": "1"}))
    await _insert(_db, "partial", _snapshot({"title": "a", "shopify_variant_id": "1"}, {"title": "b"}))

    assert [r["id"] for r in await _select()] == ["partial"]


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

async def _stamp(db, seed_id: str, variants: List[Dict[str, Any]], updated_at: Any) -> Optional[str]:
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

    assert _Client.calls == ["https://brand.com/products/serum.js"]
    assert summary["rows_with_new_ids"] == 1
    assert summary["variant_ids_stamped"] == 1
    assert summary["write_conflicts"] == 0
    after = await _seed_data(_db, "s1")
    assert after["snapshot"]["variants"][0]["shopify_variant_id"] == "41234567890123"
    assert after["snapshot"]["storefront_platform"] == "shopify"


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
