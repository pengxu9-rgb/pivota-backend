"""The nightly re-crawl queue does not spend requests on seeds of suppressed products.

`get_external_referral_refresh_candidate_seed_ids` put every active ATTACHED seed first in the
queue. Once the seed lane refuses a seed whose attached product is suppressed
(SEED_SUPPRESSED_PRODUCT_ANTI_JOIN), re-crawling it keeps an unserved price honest -- a
politeness-gated third-party request for nothing. Measured prod 2026-09-27: 682 such seeds.

Executes the real function's SQL on SQLite.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3

import pytest

import services.external_referral_readiness as module


def _sqlite(seeds, products):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE external_product_seeds (id TEXT, status TEXT, attached_product_key TEXT,"
        " domain TEXT, market TEXT, price_currency TEXT,"
        " last_crawl_attempt_at TEXT, last_crawled_at TEXT, updated_at TEXT)"
    )
    conn.execute("CREATE TABLE catalog_products (product_key TEXT, suppressed_at TEXT)")
    conn.execute(
        "CREATE TABLE catalog_source_quarantine (match_type TEXT, match_value TEXT, state TEXT,"
        " expires_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO external_product_seeds VALUES"
        " (?, 'active', ?, 'brand.com', 'US', 'USD', ?, NULL, '2026-01-01')",
        seeds,
    )
    conn.executemany("INSERT INTO catalog_products VALUES (?, ?)", products)
    return conn


def _candidates(monkeypatch, seeds, products, limit=10):
    conn = _sqlite(seeds, products)

    async def fetch_all(query, values=None):
        values = values or {}
        sql = str(query)
        ordered = re.findall(r"(?<!:):(\w+)", sql)
        cur = conn.execute(re.sub(r"(?<!:):\w+", "?", sql), [values[k] for k in ordered])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    monkeypatch.setattr(module.database, "fetch_all", fetch_all)
    return asyncio.run(module.get_external_referral_refresh_candidate_seed_ids(limit=limit))


SEEDS = [
    # (id, attached_product_key, last_crawl_attempt_at) -- the suppressed one is the STALEST,
    # so without the gate it would be first in the queue.
    ("eps_suppressed", "prod::withdrawn", None),
    ("eps_live", "prod::live", "2026-09-01"),
    ("eps_dangling", "prod::no_row", "2026-09-02"),
    ("eps_unattached", None, None),
]
PRODUCTS = [("prod::withdrawn", "2026-07-18 00:00:00"), ("prod::live", None)]


def test_a_seed_on_a_suppressed_product_is_not_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = _candidates(monkeypatch, SEEDS, PRODUCTS)
    assert "eps_suppressed" not in ids
    # Oldest attempt first, attached or not: the never-attempted unattached seed leads.
    assert ids == ["eps_unattached", "eps_live", "eps_dangling"]


def test_a_lifted_suppression_returns_the_seed_to_the_head_of_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _candidates(monkeypatch, SEEDS, [("prod::withdrawn", None), ("prod::live", None)])
    # Both never-attempted rows lead; the tie breaks on updated_at, equal here, so either order.
    assert set(ids[:2]) == {"eps_suppressed", "eps_unattached"}


def test_suppressed_seeds_do_not_consume_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate filters in SQL, before LIMIT -- not after, where 682 stale suppressed seeds at
    the head of the order would eat the whole 500-row batch and leave live seeds unrefreshed."""
    seeds = [(f"eps_s{i}", "prod::withdrawn", None) for i in range(5)] + [
        ("eps_live", "prod::live", "2026-09-01")
    ]
    ids = _candidates(monkeypatch, seeds, PRODUCTS, limit=1)
    assert ids == ["eps_live"]
