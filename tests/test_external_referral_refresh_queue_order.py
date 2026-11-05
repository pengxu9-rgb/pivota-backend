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


def _candidates(monkeypatch, seeds, *, quarantined=(), suppressed=(), limit=50, serving_regions=None):
    """`serving_regions`: PIVOTA_SERVING_PRICING_REGIONS for the run (it may only narrow the market registry's served
    markets); None leaves it unset, so the registry's list applies, as on a job that does not set it."""
    if serving_regions is None:
        monkeypatch.delenv("PIVOTA_SERVING_PRICING_REGIONS", raising=False)
    else:
        monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", serving_regions)
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


def test_a_seed_not_priced_in_a_served_currency_waits_behind_served_ones(monkeypatch) -> None:
    """A row in a currency no served region prices is not served, and a refresh refuses to switch currency, so a
    mismatched row cannot become servable by being read. It is kept, not dropped: a NULL currency can still be
    filled."""
    ids = _candidates(monkeypatch, [
        {"id": "eps_aud_in_us", "price_currency": "AUD", "last_crawl_attempt_at": None},
        {"id": "eps_null_currency", "price_currency": None, "last_crawl_attempt_at": None},
        {"id": "eps_usd", "last_crawl_attempt_at": _ts(2)},
        {"id": "eps_jpy_in_jp", "market": "JP", "price_currency": "JPY", "last_crawl_attempt_at": _ts(1)},
    ], serving_regions="US,SG")
    assert ids[:2] == ["eps_usd", "eps_jpy_in_jp"]
    assert set(ids[2:]) == {"eps_aud_in_us", "eps_null_currency"}


# SG rows live in the US partition on purpose. On the partition's currency alone, every stale SGD seed sorted behind
# every stale USD one, and with the run's limit under the stale served tier it was never read (2,696 parked seeds on
# 2026-11-05). A served pricing region's currency is a served currency.
_SG_CASE = [
    {"id": "eps_sgd_in_us", "price_currency": "SGD", "last_crawl_attempt_at": _ts(30), "last_crawled_at": _ts(30)},
    {"id": "eps_aud_in_us", "price_currency": "AUD", "last_crawl_attempt_at": _ts(40), "last_crawled_at": _ts(40)},
] + [
    {"id": f"eps_usd_{i}", "last_crawl_attempt_at": _ts(1 + i / 10), "last_crawled_at": _ts(5)} for i in range(5)
]


def test_with_sg_served_an_sgd_seed_in_the_us_partition_is_in_the_served_tier(monkeypatch) -> None:
    ids = _candidates(monkeypatch, _SG_CASE, serving_regions="US,SG", limit=3)
    # Oldest attempt first within the stale served tier: the SGD seed (30 days) heads it; AUD stays parked.
    assert ids[0] == "eps_sgd_in_us"
    assert "eps_aud_in_us" not in ids


def test_the_registry_alone_serves_sg_when_the_env_is_unset(monkeypatch) -> None:
    ids = _candidates(monkeypatch, _SG_CASE, serving_regions=None, limit=3)
    assert ids[0] == "eps_sgd_in_us"


def test_with_us_only_served_the_sgd_seed_stays_parked(monkeypatch) -> None:
    ids = _candidates(monkeypatch, _SG_CASE, serving_regions="US", limit=5)
    assert ids == [f"eps_usd_{i}" for i in (4, 3, 2, 1, 0)]


@pytest.mark.parametrize("env, expected", [("US", {"USD"}), ("US,SG", {"USD", "SGD"}), (" sg , us ", {"USD", "SGD"})])
def test_served_region_currencies_follow_the_serving_regions(monkeypatch, env, expected) -> None:
    monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", env)
    assert module.served_region_currencies() == frozenset(expected)


def test_served_region_currencies_fail_safe_to_none(monkeypatch) -> None:
    """A region config that does not load, or names no region, leaves the tier on the partition's currency alone:
    never "every currency served"."""
    import services.index_pipeline_state_service as ips

    def _boom():
        raise ValueError("registry did not load")

    monkeypatch.setattr(ips, "serving_pricing_regions", _boom)
    assert module.served_region_currencies() == frozenset()
    monkeypatch.setattr(ips, "serving_pricing_regions", lambda: [])
    assert module.served_region_currencies() == frozenset()
    # And an env that tries to WIDEN the registry raises in serving_pricing_regions: also nothing served.
    monkeypatch.undo()
    monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", "US,ZZ")
    assert module.served_region_currencies() == frozenset()


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
    """With no served currencies passed (the fail-safe default), the partition's currency alone is served."""
    assert module.refresh_queue_tier(is_fresh=is_fresh, market=market, price_currency=currency) == expected


@pytest.mark.parametrize(
    "market, currency, served, expected",
    [
        ("US", "SGD", {"USD"}, (0, 1)),
        ("US", "SGD", {"USD", "SGD"}, (0, 0)),
        ("US", " sgd ", {"USD", "SGD"}, (0, 0)),
        ("US", "AUD", {"USD", "SGD"}, (0, 1)),
        ("EU-DE", "EUR", {"USD", "SGD"}, (0, 1)),
        ("US", None, {"USD", "SGD"}, (0, 1)),
        ("US", "", {"USD", "SGD", ""}, (0, 1)),
        ("JP", "JPY", {"USD", "SGD"}, (0, 0)),
        ("SG", "USD", {"usd"}, (0, 0)),
    ],
)
def test_refresh_queue_tier_with_served_region_currencies(market, currency, served, expected) -> None:
    assert module.refresh_queue_tier(
        is_fresh=False, market=market, price_currency=currency, served_currencies=served
    ) == expected
