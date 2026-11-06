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


def _candidates(monkeypatch, seeds, *, quarantined=(), suppressed=(), limit=50, serving_regions=None,
                max_rows_per_host=None, seed_ids=None, market=None):
    """`serving_regions`: PIVOTA_SERVING_PRICING_REGIONS for the run (it may only narrow the market registry's served
    markets); None leaves it unset, so the registry's list applies, as on a job that does not set it."""
    if serving_regions is None:
        monkeypatch.delenv("PIVOTA_SERVING_PRICING_REGIONS", raising=False)
    else:
        monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", serving_regions)
    conn = sqlite3.connect(":memory:")
    conn.create_function("btrim", 1, lambda v: None if v is None else str(v).strip())  # the targeted scope's Postgres
    conn.execute(
        "CREATE TABLE external_product_seeds (id TEXT, status TEXT, attached_product_key TEXT,"
        " domain TEXT, market TEXT, price_currency TEXT, last_crawl_attempt_at TEXT,"
        " last_crawled_at TEXT, updated_at TEXT, destination_url TEXT)"
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
        row.setdefault("destination_url", f"https://{row['domain']}/products/{row['id']}")
        conn.execute(
            "INSERT INTO external_product_seeds VALUES (:id, :status, :attached_product_key,"
            " :domain, :market, :price_currency, :last_crawl_attempt_at, :last_crawled_at,"
            " :updated_at, :destination_url)",
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
    if max_rows_per_host is None:
        monkeypatch.delenv("EXTERNAL_REFERRAL_REFRESH_MAX_ROWS_PER_HOST", raising=False)
    else:
        monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MAX_ROWS_PER_HOST", max_rows_per_host)
    scope = {} if seed_ids is None else {"seed_ids": seed_ids, "market": market}
    return asyncio.run(module.get_external_referral_refresh_candidate_seed_ids(limit=limit, **scope))


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


# ---- the per-host cap on the cut (2026-11-06) ----

def _backlog(host: str, n: int, *, currency: str = "SGD", prefix: str = "eps_sg", days_ago: float = 60.0):
    """`n` never-read rows on one host, each attempted earlier than the next, so they lead the queue in id order."""
    return [
        {"id": f"{prefix}_{i:03d}", "domain": host, "price_currency": currency,
         "last_crawl_attempt_at": _ts(days_ago - i * 0.001)}
        for i in range(n)
    ]


def test_one_hosts_backlog_takes_at_most_the_cap_and_the_queue_fills_the_rest(monkeypatch) -> None:
    """The catch-up shape: an old SGD backlog on one host leads the queue. Without the cap it filled the cut; with it
    the host takes `cap` rows, in queue order, and the stale USD rows behind it get the freed slots."""
    seeds = _backlog("big.example", 6) + [
        {"id": f"eps_us_{i}", "domain": f"us{i}.example", "last_crawl_attempt_at": _ts(5 - i * 0.1),
         "last_crawled_at": _ts(9)}
        for i in range(3)
    ]
    ids = _candidates(monkeypatch, seeds, limit=5, serving_regions="US,SG", max_rows_per_host="2")
    assert ids == ["eps_sg_000", "eps_sg_001", "eps_us_0", "eps_us_1", "eps_us_2"]


def test_without_the_cap_env_the_default_still_caps(monkeypatch) -> None:
    """Unset means the default (200), never unlimited."""
    seeds = _backlog("big.example", 205) + [
        {"id": "eps_us_tail", "domain": "us.example", "last_crawl_attempt_at": _ts(5), "last_crawled_at": _ts(9)}
    ]
    ids = _candidates(monkeypatch, seeds, limit=300, serving_regions="US,SG")
    assert module._MAX_ROWS_PER_HOST_DEFAULT == 200
    assert len(ids) == 201
    assert ids[:200] == [f"eps_sg_{i:03d}" for i in range(200)]
    assert ids[-1] == "eps_us_tail"


@pytest.mark.parametrize("raw", ["", "  ", "abc", "0", "-3", "nan", "inf", "0.5"])
def test_a_bad_cap_falls_back_to_the_default_not_to_no_cap(monkeypatch, raw) -> None:
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MAX_ROWS_PER_HOST", raw)
    assert module._refresh_max_rows_per_host() == module._MAX_ROWS_PER_HOST_DEFAULT


@pytest.mark.parametrize("raw,expected", [("1", 1), ("50", 50), ("75.9", 75), (" 300 ", 300)])
def test_a_good_cap_is_read(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_MAX_ROWS_PER_HOST", raw)
    assert module._refresh_max_rows_per_host() == expected


def test_deferred_rows_keep_their_place_and_lead_the_next_cut(monkeypatch) -> None:
    """A deferred row is left out of the cut, so nothing stamps it: once the first rows have been attempted, the
    deferred ones are the oldest on the host and come next, ahead of every younger row."""
    seeds = _backlog("big.example", 4) + [
        {"id": "eps_us_young", "domain": "us.example", "last_crawl_attempt_at": _ts(1), "last_crawled_at": _ts(9)}
    ]
    first = _candidates(monkeypatch, seeds, limit=3, serving_regions="US,SG", max_rows_per_host="2")
    assert first == ["eps_sg_000", "eps_sg_001", "eps_us_young"]
    attempted = {s: _ts(0.01) for s in first}  # the run stamps what it attempted, and only that
    seeds_after = [{**s, **({"last_crawl_attempt_at": attempted[s["id"]]} if s["id"] in attempted else {})}
                   for s in seeds]
    second = _candidates(monkeypatch, seeds_after, limit=3, serving_regions="US,SG", max_rows_per_host="2")
    assert second[:2] == ["eps_sg_002", "eps_sg_003"]


def test_the_cap_keys_on_the_destination_urls_host_not_the_domain_column(monkeypatch) -> None:
    """`crawl_politeness.host_of`: lower-cased, port and userinfo dropped; a www. host is its own host, as the pacer
    keys it. The domain column plays no part."""
    seeds = [
        {"id": "eps_a", "domain": "x.example", "destination_url": "https://Shop.Example:443/p/a",
         "last_crawl_attempt_at": _ts(9)},
        {"id": "eps_b", "domain": "y.example", "destination_url": "https://u:p@shop.example/p/b",
         "last_crawl_attempt_at": _ts(8)},
        {"id": "eps_c", "domain": "shop.example", "destination_url": "https://www.shop.example/p/c",
         "last_crawl_attempt_at": _ts(7)},
        {"id": "eps_d", "domain": "shop.example", "destination_url": "https://other.example/p/d",
         "last_crawl_attempt_at": _ts(6)},
    ]
    ids = _candidates(monkeypatch, seeds, limit=10, max_rows_per_host="1")
    assert ids == ["eps_a", "eps_c", "eps_d"]


def test_rows_with_no_host_are_never_capped(monkeypatch) -> None:
    seeds = [
        {"id": f"eps_nohost_{i}", "destination_url": url, "last_crawl_attempt_at": _ts(9 - i)}
        for i, url in enumerate([None, "", "not a url", "/relative/path"])
    ]
    ids = _candidates(monkeypatch, seeds, limit=10, max_rows_per_host="1")
    assert ids == [f"eps_nohost_{i}" for i in range(4)]


def test_the_cap_applies_across_tiers_in_queue_order(monkeypatch) -> None:
    """The cap counts a host's rows in the order the cut takes them, so a host's stale rows use its slots before its
    fresh ones."""
    seeds = [
        {"id": "eps_fresh_old_attempt", "domain": "h.example", "last_crawl_attempt_at": _ts(9),
         "last_crawled_at": _ts(1)},
        {"id": "eps_stale", "domain": "h.example", "last_crawl_attempt_at": _ts(2), "last_crawled_at": _ts(9)},
        {"id": "eps_other", "domain": "o.example", "last_crawl_attempt_at": _ts(1), "last_crawled_at": _ts(1)},
    ]
    ids = _candidates(monkeypatch, seeds, limit=10, max_rows_per_host="1")
    assert ids == ["eps_stale", "eps_other"]


def test_a_targeted_operator_list_is_not_capped(monkeypatch) -> None:
    seeds = _backlog("big.example", 3, currency="USD", prefix="eps_t")
    ids = _candidates(monkeypatch, seeds, limit=10, max_rows_per_host="1",
                      seed_ids=["eps_t_000", "eps_t_001", "eps_t_002"], market="US")
    assert ids == ["eps_t_000", "eps_t_001", "eps_t_002"]


def test_the_limit_still_bounds_the_cut_under_the_cap(monkeypatch) -> None:
    seeds = [{"id": f"eps_h{i}", "domain": f"h{i}.example", "last_crawl_attempt_at": _ts(9 - i * 0.1)}
             for i in range(10)]
    assert _candidates(monkeypatch, seeds, limit=4, max_rows_per_host="1") == [f"eps_h{i}" for i in range(4)]
