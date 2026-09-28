"""Which seeds the nightly refresh reads first: served and stale, and every seed we serve.

Before: the queue took ATTACHED seeds, then unattached seeds on a connected merchant's domain,
and only when the attached rows left room under the limit, which with ~14k attached rows and a
4,000 limit they never did. The seed lanes serve unattached seeds too, so those were served on
their ingest-time price and never re-read.

Executes the real function's SQL on SQLite, with the seed lane's own quarantine and suppression
fragments.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import services.external_referral_readiness as module

NOW = datetime.now(timezone.utc)


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _candidates(monkeypatch, seeds, *, quarantined=(), suppressed=(), limit=50):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE external_product_seeds (id TEXT, status TEXT, attached_product_key TEXT,"
        " domain TEXT, market TEXT, price_currency TEXT, last_crawl_attempt_at TEXT,"
        " last_crawled_at TEXT, updated_at TEXT)"
    )
    conn.execute("CREATE TABLE catalog_products (product_key TEXT, suppressed_at TEXT)")
    conn.execute(
        "CREATE TABLE catalog_source_quarantine (match_type TEXT, match_value TEXT, state TEXT,"
        " expires_at TEXT)"
    )
    for seed in seeds:
        row = {
            "status": "active", "attached_product_key": None, "domain": "brand.com",
            "market": "US", "price_currency": "USD", "last_crawl_attempt_at": None,
            "last_crawled_at": None, "updated_at": "2026-01-01 00:00:00", **seed,
        }
        conn.execute(
            "INSERT INTO external_product_seeds VALUES (:id, :status, :attached_product_key,"
            " :domain, :market, :price_currency, :last_crawl_attempt_at, :last_crawled_at,"
            " :updated_at)",
            row,
        )
    conn.executemany(
        "INSERT INTO catalog_products VALUES (?, '2026-09-01 00:00:00')", [(k,) for k in suppressed]
    )
    conn.executemany(
        "INSERT INTO catalog_source_quarantine VALUES ('domain', ?, 'active', NULL)",
        [(d,) for d in quarantined],
    )

    async def fetch_all(query, values=None):
        values = dict(values or {})
        for key, value in values.items():
            if isinstance(value, datetime):
                values[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        sql = str(query)
        ordered = re.findall(r"(?<!:):(\w+)", sql)
        cur = conn.execute(re.sub(r"(?<!:):\w+", "?", sql), [values[k] for k in ordered])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    monkeypatch.setattr(module.database, "fetch_all", fetch_all)
    monkeypatch.delenv("EXTERNAL_REFERRAL_REFRESH_FRESH_HOURS", raising=False)
    return asyncio.run(module.get_external_referral_refresh_candidate_seed_ids(limit=limit))


def test_an_unattached_seed_on_any_domain_is_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old unattached branch required a connected merchant store's domain. Seed lanes serve
    unattached seeds on any domain."""
    ids = _candidates(monkeypatch, [
        {"id": "eps_attached", "attached_product_key": "p1", "last_crawl_attempt_at": _ts(3)},
        {"id": "eps_unattached", "domain": "no-store.example", "last_crawl_attempt_at": _ts(9)},
    ])
    assert ids == ["eps_unattached", "eps_attached"]


def test_stale_seeds_go_before_fresh_ones_whatever_their_attempt_clock(monkeypatch) -> None:
    """A seed read an hour ago can have the OLDEST attempt stamp only if nothing else was tried
    since, and it still does not need a read. Stale beats fresh; attempts order within a tier."""
    ids = _candidates(monkeypatch, [
        {"id": "eps_fresh", "last_crawl_attempt_at": _ts(1.0), "last_crawled_at": _ts(1.0)},
        {"id": "eps_stale_read", "last_crawl_attempt_at": _ts(0.5), "last_crawled_at": _ts(9)},
        {"id": "eps_never_read", "last_crawl_attempt_at": _ts(0.1)},
    ])
    assert ids == ["eps_stale_read", "eps_never_read", "eps_fresh"]


def test_the_fresh_window_is_48_hours_by_default_and_tunable(monkeypatch) -> None:
    seeds = [
        {"id": "eps_read_1d", "last_crawl_attempt_at": _ts(5), "last_crawled_at": _ts(1)},
        {"id": "eps_read_3d", "last_crawl_attempt_at": _ts(3), "last_crawled_at": _ts(3)},
    ]
    assert _candidates(monkeypatch, seeds) == ["eps_read_3d", "eps_read_1d"]
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_FRESH_HOURS", "0")
    # 0h: nothing is fresh, so the attempt clock alone decides.
    ids = asyncio.run(module.get_external_referral_refresh_candidate_seed_ids(limit=5))
    assert ids == ["eps_read_1d", "eps_read_3d"]


def test_a_seed_not_priced_in_its_markets_currency_waits_behind_served_ones(monkeypatch) -> None:
    """The seed lane serves a row only in its market's currency, and a refresh refuses to switch
    currency, so a mismatched row cannot become servable by being read. It is kept, not dropped:
    a NULL currency can still be filled."""
    ids = _candidates(monkeypatch, [
        {"id": "eps_sgd_in_us", "price_currency": "SGD", "last_crawl_attempt_at": None},
        {"id": "eps_null_currency", "price_currency": None, "last_crawl_attempt_at": None},
        {"id": "eps_usd", "last_crawl_attempt_at": _ts(2)},
        {"id": "eps_jpy_in_jp", "market": "JP", "price_currency": "JPY", "last_crawl_attempt_at": _ts(1)},
    ])
    assert ids[:2] == ["eps_usd", "eps_jpy_in_jp"]
    assert set(ids[2:]) == {"eps_sgd_in_us", "eps_null_currency"}


def test_quarantined_suppressed_and_retired_seeds_are_not_candidates(monkeypatch) -> None:
    ids = _candidates(
        monkeypatch,
        [
            {"id": "eps_quarantined", "domain": "www.Bad.com"},
            {"id": "eps_suppressed", "attached_product_key": "p_withdrawn"},
            {"id": "eps_retired", "status": "inactive"},
            {"id": "eps_live", "last_crawl_attempt_at": _ts(1)},
        ],
        quarantined=["bad.com"],
        suppressed=["p_withdrawn"],
    )
    assert ids == ["eps_live"]


def test_the_limit_applies_after_the_tiers(monkeypatch) -> None:
    seeds = [
        {"id": f"eps_fresh_{i}", "last_crawl_attempt_at": _ts(20 + i), "last_crawled_at": _ts(0.5)}
        for i in range(5)
    ] + [{"id": "eps_stale", "last_crawl_attempt_at": _ts(1), "last_crawled_at": _ts(30)}]
    assert _candidates(monkeypatch, seeds, limit=1) == ["eps_stale"]


@pytest.mark.parametrize(
    "is_fresh, market, currency, expected",
    [
        (False, "US", "USD", (0, 0)),
        (False, "US", " usd ", (0, 0)),
        (False, None, "USD", (0, 0)),
        (False, "US", "SGD", (0, 1)),
        (False, "EU-DE", "EUR", (0, 1)),
        (True, "JP", "JPY", (1, 0)),
        (True, "US", None, (1, 1)),
    ],
)
def test_refresh_queue_tier(is_fresh, market, currency, expected) -> None:
    assert module.refresh_queue_tier(is_fresh=is_fresh, market=market, price_currency=currency) == expected
