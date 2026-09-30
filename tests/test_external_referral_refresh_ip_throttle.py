"""The IP-level throttle breaker and the 429/503 diagnostics, through the real refresh path.

2026-09-30 (execution hbr9g): 1,177 429s across 331 distinct hosts from the first minute, every
backed-off host Shopify-served, ulta and theordinary reading fine. Shopify's shared edge was
throttling our one crawl-egress IP; the per-host breaker tripped 261 times, one host at a time,
and the night read as a generic "degraded".

Driven here: the REAL batch scheduler (`run_external_referral_refresh_batch`), the REAL fetch
(`external_offers_service._fetch_html` -> `crawl_politeness.note_response` -> the installed
`crawl_ip_throttle` breaker), with only the HTTP layer stubbed (`httpx.MockTransport`). The
refresher maps a fetch exactly the way `_refresh_external_seed_by_id` does for the batch: a
non-2xx becomes `{"status": "degraded", "error": "destination_unavailable: http N"}`. Calling
the refresher is what stamps `last_crawl_attempt_at` in production, so "never called" is
"never stamped".
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

import jobs.external_referral_refresh as job
import services.crawl_ip_throttle as cit
import services.crawl_politeness as cp
import services.external_offers_service as svc
import services.external_referral_readiness as err

# Captured once: a test that drives twice must not wrap its own stub in a second stub.
_REAL_ASYNC_CLIENT = httpx.AsyncClient

SHOPIFY = {"powered-by": "Shopify", "server": "cloudflare", "x-shopid": "5571"}
PLAIN = {"server": "nginx"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    cp.reset_for_tests()
    cit.reset_for_tests()
    # No robots fetch, and no real sleeps: pacing and backoff still RUN, at zero length.
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "0")
    for name in (
        "CRAWL_IP_THROTTLE_TRIP_HOSTS",
        "CRAWL_IP_THROTTLE_WINDOW_SECONDS",
        "CRAWL_IP_THROTTLE_BREAKER_ENABLED",
        "EXTERNAL_REFERRAL_REFRESH_HOST_BLOCK_TRIP",
        "EXTERNAL_REFERRAL_REFRESH_HOST_UNREACHABLE_TRIP",
        "EXTERNAL_REFERRAL_REFRESH_HOST_CONCURRENCY",
        "EXTERNAL_REFERRAL_REFRESH_MIN_ORIGIN_YIELD",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    cp.reset_for_tests()
    cit.reset_for_tests()


# A scripted answer: (status, headers, clock offset in seconds or None to leave the clock alone).
Answer = Tuple[int, Dict[str, str], Optional[float]]


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    rows: List[Tuple[str, Answer]],
    *,
    concurrency: Optional[int] = None,
    fake_clock: bool = False,
    **batch_kwargs: Any,
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Run the real batch over `rows` (host, answer), one seed per row, in queue order.

    Returns (summary, seed ids the refresher was called for = stamped, all seed ids).
    """
    seed_ids = [f"eps_{i}" for i in range(len(rows))]
    url_of = {s: f"https://{host}/products/p{i}" for i, (s, (host, _)) in enumerate(zip(seed_ids, rows))}
    answer_of = {url_of[s]: answer for s, (_host, answer) in zip(seed_ids, rows)}
    monkeypatch.setattr(
        err, "get_external_referral_refresh_candidate_seed_ids",
        lambda *a, **k: asyncio.sleep(0, result=seed_ids),
    )
    monkeypatch.setattr(
        err, "_fetch_refresh_candidate_hosts",
        lambda ids: asyncio.sleep(0, result={s: cp.host_of(url_of[s]) for s in ids}),
    )
    clock = {"t": 0.0}
    if fake_clock:
        monkeypatch.setattr(cit, "_now", lambda: clock["t"])

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, at = answer_of[str(request.url)]
        if at is not None:
            clock["t"] = at
        return httpx.Response(status, headers=headers, content=b"<html></html>", request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        svc.httpx, "AsyncClient", lambda *a, **kw: _REAL_ASYNC_CLIENT(*a, transport=transport, **kw)
    )
    stamped: List[str] = []

    async def refresh(seed_id: str) -> Dict[str, Any]:
        stamped.append(seed_id)
        url = url_of[seed_id]
        try:
            await svc._fetch_html(url, max_wait=0)
        except svc.ExternalOfferUnavailable as exc:
            return {
                "status": "degraded",
                "error": f"destination_unavailable: http {exc.status_code}",
                "domain": cp.host_of(url),
            }
        return {"status": "success", "price_refresh": {"status": "unchanged"}, "projection": {}}

    summary = asyncio.run(err.run_external_referral_refresh_batch(
        refresh_seed_by_id=refresh, limit=len(rows), host_concurrency=concurrency, **batch_kwargs,
    ))
    return summary, stamped, seed_ids


def _hosts_called(rows, stamped, seed_ids) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for seed_id in stamped:
        host = rows[seed_ids.index(seed_id)][0]
        out[host] = out.get(host, 0) + 1
    return out


THROTTLED = (429, dict(SHOPIFY, **{"retry-after": "60"}), None)
SHOP_OK = (200, SHOPIFY, None)
PLAIN_OK = (200, PLAIN, None)


def _storm_rows(shopify_hosts: int = 20) -> List[Tuple[str, Answer]]:
    """The 09-30 shape, round-major so every host's first row comes before anyone's second:
    one Shopify host that read fine before the storm (`known.shop`), `shopify_hosts` Shopify
    hosts answering 429 + Retry-After: 60, and ulta (not Shopify) reading fine throughout."""
    rows: List[Tuple[str, Answer]] = []
    for _round in range(3):
        rows.append(("known.shop", SHOP_OK))
        for i in range(shopify_hosts):
            rows.append((f"s{i}.shop", THROTTLED))
            rows.append(("www.ulta.com", PLAIN_OK))
    return rows


# --------------------------------------------------------------------- the 09-30 storm


def test_the_09_30_storm_trips_and_non_shopify_hosts_carry_on(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _storm_rows()
    summary, stamped, seed_ids = _drive(monkeypatch, rows, concurrency=1)
    called = _hosts_called(rows, stamped, seed_ids)

    assert summary["ip_throttled"] is True
    assert summary["status"] == "ip_throttled"
    assert summary["ip_throttle_trip_host_count"] == 10, "trips on the 10th distinct host"
    assert summary["ip_throttle_tripped_at"] and summary["ip_throttle_first_429_at"]
    assert summary["ip_throttle_last_429_at"] >= summary["ip_throttle_first_429_at"]
    # Every ulta row was still read: the breaker holds back Shopify-served hosts only.
    assert called["www.ulta.com"] == 60
    # A Shopify host KNOWN from an earlier 200 is held back at take time, with no row in flight.
    assert called["known.shop"] == 1
    # Each throttled Shopify host was asked exactly once: s0..s9 before the trip, s10..s19 on
    # first contact (unknown until they answer), then their lanes drained.
    assert all(called[f"s{i}.shop"] == 1 for i in range(20)), called
    assert summary["skipped_for_ip_throttle"] == 2 + 20 * 2
    assert set(summary["ip_throttle_skips"].values()) == {2}, "top 10 hosts, two rows each"
    # The per-host breaker never saw a streak: this is the IP breaker's doing, not its.
    assert summary["skipped_for_host_backoff"] == 0
    # Skipped rows are out of the yield denominator, like the other breakers' skips.
    assert summary["attempted_count"] == len(stamped) == len(rows) - 42


def test_skipped_rows_are_never_handed_to_the_refresher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handing a seed to the refresher is what stamps `last_crawl_attempt_at`. A row the IP
    breaker held back must not be stamped, so it sorts first (NULLS FIRST / oldest) tomorrow."""
    rows = _storm_rows()
    summary, stamped, seed_ids = _drive(monkeypatch, rows, concurrency=1)
    held = [s for s in seed_ids if s not in stamped]
    assert len(held) == summary["skipped_for_ip_throttle"] == 42
    assert len(set(stamped)) == len(stamped), "no row stamped twice"
    assert {rows[seed_ids.index(s)][0] for s in held} <= (
        {"known.shop"} | {f"s{i}.shop" for i in range(20)}
    ), "only Shopify-served rows are held back"


def test_the_storm_trips_with_four_workers_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod runs `--host-concurrency 4`. Order is no longer exact; the invariants are."""
    rows = _storm_rows(30)
    summary, stamped, seed_ids = _drive(monkeypatch, rows, concurrency=4)
    called = _hosts_called(rows, stamped, seed_ids)
    assert summary["status"] == "ip_throttled"
    assert called["www.ulta.com"] == 90
    assert max(n for h, n in called.items() if h.endswith(".shop")) <= 2
    assert summary["skipped_for_ip_throttle"] > 0
    assert len(stamped) + summary["skipped_for_ip_throttle"] == len(rows)


# --------------------------------------------------------------------- a healthy day

# Prod 2026-09-29, execution external-referral-refresh-zxx62: every `crawl backoff` line of the
# run as (seconds after the first, host, status). 42 lines, 17 hosts, all at :00-:04 of a minute.
_SEPT_29 = [
    (0, "www.wetnwildbeauty.com", 429), (0, "addros.com", 429), (0, "universalnailsupplies.com", 429),
    (121, "ikatehouse.com", 429), (181, "www.kissusa.com", 429), (181, "www.impressbeauty.com", 429),
    (213, "themedicube.us.com", 503), (215, "themedicube.us.com", 503),
    (219, "themedicube.us.com", 503), (228, "themedicube.us.com", 503),
    (300, "biodance.com", 429), (360, "www.kissusa.com", 429), (421, "cocodor.com", 429),
    (480, "www.kissusa.com", 429), (541, "holiholic.com", 429), (601, "www.kissusa.com", 429),
    (661, "holiholic.com", 429), (782, "holiholic.com", 429), (901, "nanasbeautyholic.com", 429),
    (960, "koolseoul.com", 429), (960, "kbeautymakeup.com", 429), (1020, "nanasbeautyholic.com", 429),
    (1080, "kbeautymakeup.com", 429), (1140, "nanasbeautyholic.com", 429), (1141, "ichibanm.com", 429),
    (1260, "ichibanm.com", 429), (1321, "www.imagebeauty.com", 429), (1380, "ichibanm.com", 429),
    (1500, "tripletraders.com", 429), (1560, "ichibanm.com", 429), (1680, "ichibanm.com", 429),
    (1801, "ichibanm.com", 429), (1921, "ichibanm.com", 429), (2041, "ichibanm.com", 429),
    (2220, "ichibanm.com", 429), (2340, "ichibanm.com", 429), (2460, "ichibanm.com", 429),
    (2580, "ichibanm.com", 429), (2701, "ichibanm.com", 429), (2880, "ichibanm.com", 429),
    (3000, "ichibanm.com", 429), (3181, "eyurs.com", 429),
]


def test_a_09_29_day_never_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replayed at its real timing, WORST CASE: every host Shopify-served and every 503 carrying
    a Retry-After, so all 42 count toward the trip. Each 429 is followed by a read on the same
    host, as it was in prod, so the per-host breaker stays out of it. Peak: 3 hosts in 60s."""
    rows: List[Tuple[str, Answer]] = []
    for at, host, code in _SEPT_29:
        rows.append((host, (code, dict(SHOPIFY, **{"retry-after": "60"}), float(at))))
        rows.append((host, (200, SHOPIFY, float(at) + 0.5)))
    summary, stamped, _ = _drive(monkeypatch, rows, concurrency=1, fake_clock=True)
    assert summary["ip_throttled"] is False
    assert summary["status"] == "success"
    assert summary["skipped_for_ip_throttle"] == 0
    assert len(stamped) == len(rows)
    assert summary["ip_throttle_hosts"] == 17
    assert summary["ip_throttle_peak_shopify_hosts_in_window"] == 3
    assert summary["throttle_diagnostics"]["responses"] == 42


def test_a_single_hot_host_is_the_per_host_breakers_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thirty 429s from ONE Shopify host is one host throttling us, not the edge. With the
    per-host breaker off so every row really is asked, the IP breaker still never trips."""
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_BLOCK_TRIP", "0")
    rows = [("fentybeauty.com", THROTTLED)] * 30 + [("www.ulta.com", PLAIN_OK)] * 5
    summary, stamped, _ = _drive(monkeypatch, rows, concurrency=1)
    assert summary["ip_throttled"] is False
    assert summary["ip_throttle_peak_shopify_hosts_in_window"] == 1
    assert summary["throttle_diagnostics"]["responses"] == 30
    assert len(stamped) == 35


def test_non_shopify_hosts_are_counted_but_never_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The all-hosts count is evidence, not a trigger: 15 non-Shopify hosts 429ing in one minute
    is not Shopify's edge, and stopping them would be guessing."""
    rows = [(f"h{i}.example", (429, {"server": "nginx", "retry-after": "5"}, None)) for i in range(15)]
    summary, stamped, _ = _drive(monkeypatch, rows, concurrency=1)
    assert summary["ip_throttled"] is False
    assert summary["ip_throttle_peak_hosts_in_window"] == 15
    assert summary["ip_throttle_peak_shopify_hosts_in_window"] == 0


def test_a_503_counts_only_with_a_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare 503 is an outage; a 503 that says when to come back is a throttle."""
    bare = [(f"b{i}.shop", (503, SHOPIFY, None)) for i in range(12)]
    summary, _, _ = _drive(monkeypatch, bare, concurrency=1)
    assert summary["ip_throttled"] is False
    assert summary["throttle_diagnostics"]["responses"] == 12
    cp.reset_for_tests()
    timed = [(f"t{i}.shop", (503, dict(SHOPIFY, **{"retry-after": "30"}), None)) for i in range(12)]
    summary, _, _ = _drive(monkeypatch, timed, concurrency=1)
    assert summary["ip_throttled"] is True


def test_a_shopify_host_is_recognised_from_an_earlier_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 page can carry fewer headers than the product page the same host served a minute
    earlier. Shopify-ness learned from the 200 sticks to the host."""
    rows = [(f"k{i}.shop", SHOP_OK) for i in range(10)]
    rows += [(f"k{i}.shop", (429, {"server": "cloudflare", "retry-after": "60"}, None)) for i in range(10)]
    summary, _, _ = _drive(monkeypatch, rows, concurrency=1)
    assert summary["ip_throttled"] is True


# --------------------------------------------------------------------- tuning and the kill switch


def test_the_kill_switch_env_disables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWL_IP_THROTTLE_BREAKER_ENABLED", "false")
    rows = _storm_rows()
    summary, stamped, _ = _drive(monkeypatch, rows, concurrency=1)
    assert summary["ip_throttled"] is False and summary["ip_throttle_breaker_armed"] is False
    assert summary["status"] != "ip_throttled"
    assert summary["skipped_for_ip_throttle"] == 0
    assert len(stamped) == len(rows)
    # Diagnostics still collected with the breaker off.
    assert summary["throttle_diagnostics"]["responses"] == 60
    assert summary["ip_throttle_peak_shopify_hosts_in_window"] >= 10


def test_the_kill_switch_argument_and_a_zero_trip_disable_it(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, _, _ = _drive(monkeypatch, _storm_rows(), concurrency=1, ip_throttle_enabled=False)
    assert summary["ip_throttled"] is False
    cp.reset_for_tests()
    monkeypatch.setenv("CRAWL_IP_THROTTLE_TRIP_HOSTS", "0")
    summary, _, _ = _drive(monkeypatch, _storm_rows(), concurrency=1)
    assert summary["ip_throttled"] is False and summary["ip_throttle_breaker_armed"] is False


def test_the_trip_and_window_are_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, _, _ = _drive(monkeypatch, _storm_rows(), concurrency=1, ip_throttle_trip_hosts=5)
    assert summary["ip_throttle_trip_host_count"] == 5
    cp.reset_for_tests()
    monkeypatch.setenv("CRAWL_IP_THROTTLE_TRIP_HOSTS", "7")
    monkeypatch.setenv("CRAWL_IP_THROTTLE_WINDOW_SECONDS", "30")
    summary, _, _ = _drive(monkeypatch, _storm_rows(), concurrency=1)
    assert summary["ip_throttle_trip_host_count"] == 7
    assert summary["ip_throttle_window_seconds"] == 30.0


def test_the_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twelve Shopify hosts 429ing a minute and a half apart each never share a window."""
    rows = [(f"w{i}.shop", (429, SHOPIFY, 90.0 * i)) for i in range(12)]
    summary, _, _ = _drive(monkeypatch, rows, concurrency=1, fake_clock=True)
    assert summary["ip_throttled"] is False
    assert summary["ip_throttle_peak_shopify_hosts_in_window"] == 1


# --------------------------------------------------------------------- the headers


def test_throttle_headers_are_logged_truncated_and_never_cookies(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    long_server = "x" * 200
    headers = {
        "Retry-After": "60",
        "Server": long_server,
        "Powered-By": "Shopify",
        "CF-Ray": "8c1d2e3f4a5b6c7d-SJC",
        "CF-Mitigated": "challenge",
        "X-ShopId": "5571",
        "X-Shopify-Stage": "production",
        "Set-Cookie": "_secure_session_id=SECRET-SESSION; Path=/",
        "Authorization": "Bearer SECRET-TOKEN",
    }
    caplog.set_level(logging.WARNING, logger="services.crawl_politeness")
    summary, _, _ = _drive(monkeypatch, [("brand.shop", (429, headers, None))], concurrency=1)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("crawl backoff")]
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("crawl backoff: brand.shop returned 429 (consecutive=1), holding")
    for fragment in ("retry-after=60", "powered-by=Shopify", "cf-ray=8c1d2e3f4a5b6c7d-SJC",
                     "cf-mitigated=challenge", "x-shopid=5571", "x-shopify-stage=production"):
        assert fragment in line
    assert f"server={'x' * 80} " in line and "x" * 81 not in line
    assert "SECRET" not in line and "cookie" not in line.lower() and "authorization" not in line.lower()

    diag = summary["throttle_diagnostics"]
    assert diag["by_powered_by"] == {"shopify": 1}
    assert diag["by_server"] == {"x" * 80: 1}
    assert diag["retry_after"] == {"60": 1}
    assert diag["cf_mitigated"] == 1
    assert "SECRET" not in repr(summary)


def test_capture_is_an_allowlist_and_strips_line_breaks() -> None:
    got = cit.capture_throttle_headers(
        {"retry-after": "60\r\nforged: line", "cookie": "a=b", "x-other": "1", "SERVER": " cloudflare "}
    )
    assert got == {"retry-after": "60  forged: line", "server": "cloudflare"}
    assert cit.capture_throttle_headers(None) == {}


@pytest.mark.parametrize("diag, expected", [
    ({"powered-by": "Shopify"}, True),
    ({"x-powered-by": "Shopify"}, True),
    ({"server": "Shopify"}, True),
    ({"x-shopid": "5571"}, True),
    ({"x-shopify-stage": "production"}, True),
    # Shopify fronts with Cloudflare, and so do thousands of sites that share no limit with it.
    ({"server": "cloudflare", "cf-ray": "8c1d-SJC"}, False),
    ({"x-powered-by": "Next.js"}, False),
    ({}, False),
])
def test_what_counts_as_shopify_served(diag, expected) -> None:
    assert cit.looks_shopify_served(diag) is expected


def test_a_line_with_no_headers_is_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    """Callers that do not pass headers yet keep the exact line they logged before."""
    caplog.set_level(logging.WARNING, logger="services.crawl_politeness")
    cp.note_response("https://a.com/x", 429)
    assert caplog.records[-1].getMessage() == "crawl backoff: a.com returned 429 (consecutive=1), holding 0.0s"


# --------------------------------------------------------------------- exit code


def _main_with(monkeypatch: pytest.MonkeyPatch, summary: Dict[str, Any], argv: List[str] = ()):
    seen: Dict[str, Any] = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return summary

    monkeypatch.setattr(job, "run_daily_external_referral_refresh", fake_run)
    monkeypatch.setattr(job.database, "connect", lambda: asyncio.sleep(0))
    monkeypatch.setattr(job.database, "disconnect", lambda: asyncio.sleep(0))
    monkeypatch.setattr("sys.argv", ["external_referral_refresh", "--limit", "1", *argv])
    return job.main(), seen


def test_an_ip_throttled_run_exits_with_its_own_code(monkeypatch, caplog, capsys) -> None:
    caplog.set_level(logging.ERROR, logger="jobs.external_referral_refresh")
    code, _ = _main_with(monkeypatch, {
        "status": "ip_throttled", "status_without_ip_throttle": "degraded",
        "ip_throttled": True, "ip_throttle_trip_host_count": 10, "skipped_for_ip_throttle": 42,
    })
    assert code == job.EXIT_IP_THROTTLED == 3
    assert any("finished ip_throttled" in r.getMessage() for r in caplog.records)
    out = capsys.readouterr().out
    assert 'IP_THROTTLE {"ip_throttled":true' in out


def test_real_degradation_still_exits_one(monkeypatch) -> None:
    code, _ = _main_with(monkeypatch, {"status": "degraded", "ip_throttled": False})
    assert code == job.EXIT_DEGRADED == 1


def test_the_cli_reaches_the_batch(monkeypatch) -> None:
    _, seen = _main_with(monkeypatch, {"status": "success"}, [
        "--ip-throttle-trip-hosts", "12", "--ip-throttle-window-seconds", "90",
        "--no-ip-throttle-breaker",
    ])
    assert seen["ip_throttle_trip_hosts"] == 12
    assert seen["ip_throttle_window_seconds"] == 90.0
    assert seen["ip_throttle_enabled"] is False
    _, seen = _main_with(monkeypatch, {"status": "success"})
    assert seen["ip_throttle_enabled"] is None, "no flag leaves the env var in charge"


def test_the_breaker_is_uninstalled_after_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A breaker left installed would keep collecting (and blocking) for the next run in the
    same process."""
    _drive(monkeypatch, _storm_rows(), concurrency=1)
    assert cit._ACTIVE == []
