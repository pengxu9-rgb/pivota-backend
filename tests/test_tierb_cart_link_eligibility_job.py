"""jobs/tierb_cart_link_eligibility.py: the gate, the no-buyer guarantee, pacing, the budget,
retries, dry-run, exit codes, and a mocked end-to-end run into the real table.

DIALECT-AGNOSTIC: the database cases run on whichever engine DATABASE_URL names, SQLite by
default; tests/test_tierb_cart_link_eligibility_postgres.py re-runs them on Postgres.

NO REAL NETWORK. `_no_real_network` refuses every request that is not on a MockTransport — and
because the job deliberately survives an exception inside one merchant's preflight (it counts it
as a crash and moves on), the fixture also RECORDS each refusal and fails the test at teardown.
A refusal that only raised would be swallowed into "crashed" and the test could still pass.

TIME IS FAKE. `FakeClock.sleep` advances the clock instead of waiting, so pacing and the budget
are measured exactly, and the suite does not take 1.5 s per request.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jobs.tierb_cart_link_eligibility as job  # noqa: E402
from db.database import database  # noqa: E402
import db.tierb_cart_link_eligibility as elig  # noqa: E402
from services.shopify_cart_link_preflight import PreflightResult, Verdict  # noqa: E402
from services.tierb_cart_link_merchants import Merchant, MerchantListError  # noqa: E402

TABLE = "tierb_cart_link_eligibility"
ON = {job.GATE_ENV: "true"}


# ── fixtures ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    refused: List[str] = []

    async def refuse_async(self, request):
        refused.append(f"{request.method} {request.url.host}")
        raise AssertionError(f"un-mocked network request: {request.method} {request.url.host}")

    def refuse_sync(self, request):
        refused.append(f"{request.method} {request.url.host}")
        raise AssertionError(f"un-mocked network request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_sync)
    yield refused
    assert not refused, f"un-mocked network requests were attempted: {refused}"


@pytest.fixture
async def db():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await elig.ensure_schema()
    yield
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    if not was_connected and database.is_connected:
        await database.disconnect()


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


# ── a routed fake storefront ────────────────────────────────────────────────────────────────

VIDS = {
    "judydoll.com": "50041364447509",
    "podl.us": "47000000000001",
    "luafee.jp": "47000000000002",
    "flaky.us": "47000000000003",
    "extra1.us": "47000000000004",
    "extra2.us": "47000000000005",
    "extra3.us": "47000000000006",
    "extra4.us": "47000000000007",
}
TOKEN = "hWNGxZONBs9DRwvyX0myoSvM"


class Storefronts:
    """kind per host: eligible | login | not_accepting | mismatch | transport. Every request is recorded
    with the fake clock's time, so pacing is measured on what actually reached the network."""

    def __init__(self, clock: FakeClock, kinds: Dict[str, str]) -> None:
        self.clock = clock
        self.kinds = kinds
        self.requests: List[Tuple[float, httpx.Request]] = []
        self.click_ids: Dict[str, str] = {}
        self.countries: Dict[str, str] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.clock.now, request))
        host, path = request.url.host, request.url.path
        if host == "shopify.com":
            return httpx.Response(406, text="<html>Sign in</html>")
        kind = self.kinds[host]
        if kind == "transport":
            raise httpx.ConnectError("proxy flake", request=request)
        vid = VIDS[host]
        if path == "/products.json":
            return httpx.Response(200, json={"products": [{
                "title": "Single Eyeshadow", "handle": "single-eyeshadow",
                "variants": [{"id": int(vid), "title": "#D02", "available": True, "price": "9.99",
                              "requires_shipping": True}],
            }]})
        if path.startswith("/cart/"):
            query = dict(parse_qsl(urlparse(str(request.url)).query))
            self.click_ids[host] = query.get("attributes[pivota_click_id]", "")
            # The permalink pins the checkout market (`country=`); Shopify honours it, except
            # on the "mismatch" store, which lands the buyer in JP whatever was asked.
            self.countries[host] = "JP" if kind == "mismatch" else query.get("country", "")
            if kind == "login":
                return httpx.Response(302, headers={"location": "https://shopify.com/authentication/1/login"})
            return httpx.Response(302, headers={"location": f"https://{host}/checkouts/cn/{TOKEN}/information"})
        if path.startswith("/checkouts/cn/"):
            if kind == "not_accepting":
                return httpx.Response(403, text="<h1>This store isn&rsquo;t set up to receive orders yet</h1>")
            return httpx.Response(200, text=self.checkout_page(host, vid),
                                  headers={"content-type": "text/html; charset=utf-8"})
        raise AssertionError(f"unrouted {host}{path}")

    def checkout_page(self, host: str, vid: str) -> str:
        """The checkout's serialized state, keyed the way the live page keys it (the shape
        #2209's structural readers parse): a MerchandiseLine for the variant, the click id as a
        cart attribute, and the buyer's country."""
        state = {
            "merchandise": {"merchandiseLines": [{
                "__typename": "MerchandiseLine",
                "merchandise": {"__typename": "ProductVariantMerchandise",
                                "id": f"gid://shopify/ProductVariantMerchandise/{vid}",
                                "variantId": f"gid://shopify/ProductVariant/{vid}"},
            }]},
            "note": {"customAttributes": [{"key": "pivota_click_id", "value": self.click_ids.get(host, "")}]},
            "buyerIdentity": {"customer": {"countryCode": self.countries.get(host, "")}},
        }
        serialized = html.escape(json.dumps(state, separators=(",", ":")), quote=True)
        return f'<html><head><meta name="serialized-session" content="{serialized}"></head><body></body></html>'

    def times(self) -> List[float]:
        return [t for t, _ in self.requests]

    def urls(self) -> List[str]:
        return [str(r.url) for _, r in self.requests]


def merchants(*domains: str, market: str = "US") -> List[Merchant]:
    return [Merchant(d, market, VIDS[d]) for d in domains]


def result(verdict: str, host: str = "judydoll.com", **over) -> PreflightResult:
    v = Verdict(verdict)
    return PreflightResult(host=host, verdict=v, retryable=v is Verdict.TRANSPORT_ERROR, market="US", **over)


class SpyPreflight:
    """A stand-in preflight: returns scripted results per host, records every call's kwargs."""

    def __init__(self, script: Dict[str, List[str]], default: str = "ELIGIBLE") -> None:
        self.script = {h: list(v) for h, v in script.items()}
        self.default = default
        self.calls: List[Tuple[str, dict]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, host, **kwargs):
        self.calls.append((host, kwargs))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            queue = self.script.get(host)
            verdict = queue.pop(0) if queue else self.default
            return result(verdict, host=host)
        finally:
            self.in_flight -= 1


class SpyRecorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: List[Tuple[str, str, PreflightResult]] = []
        self.fail = fail

    async def __call__(self, domain, market, res):
        self.calls.append((domain, market, res))
        if self.fail:
            raise RuntimeError("db down")
        return {}


async def run(**kw):
    clock = kw.pop("clock", None) or FakeClock()
    kw.setdefault("environ", ON)
    kw.setdefault("clock", clock)
    kw.setdefault("sleep", clock.sleep)
    kw.setdefault("emit", lambda line: None)
    kw.setdefault("stamp", "T")
    return await job.run(**kw)


# ── the gate ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
def test_the_gate_accepts_only_the_truthy_spellings(value):
    assert job.gate_enabled({job.GATE_ENV: value}) is True


@pytest.mark.parametrize("value", [None, "", " ", "0", "false", "no", "off", "tru", "enabled", "2", "y", "true!"])
def test_the_gate_refuses_everything_else(value):
    env = {} if value is None else {job.GATE_ENV: value}
    assert job.gate_enabled(env) is False


@pytest.mark.parametrize("dry_run", [False, True])
async def test_gate_off_does_nothing_exits_zero_and_says_so_at_warning(dry_run, caplog):
    spy, rec = SpyPreflight({}), SpyRecorder()
    with caplog.at_level(logging.WARNING, logger=job.__name__):
        summary = await run(environ={}, dry_run=dry_run, merchants=merchants("judydoll.com"),
                            preflight_fn=spy, record_fn=rec)
    assert summary.gate_enabled is False
    assert summary.exit_code == 0
    assert spy.calls == [] and rec.calls == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and job.GATE_ENV in r.getMessage()]
    assert warnings, "gate-off must be logged at WARNING"


async def test_the_gate_is_read_from_the_process_environment_inside_the_run(monkeypatch):
    spy = SpyPreflight({})
    monkeypatch.delenv(job.GATE_ENV, raising=False)
    off = await run(environ=None, dry_run=True, merchants=merchants("judydoll.com"), preflight_fn=spy)
    assert off.gate_enabled is False and spy.calls == []
    monkeypatch.setenv(job.GATE_ENV, "true")
    on = await run(environ=None, dry_run=True, merchants=merchants("judydoll.com"), preflight_fn=spy)
    assert on.gate_enabled is True and len(spy.calls) == 1


def test_main_with_the_gate_off_exits_zero_without_touching_the_database(monkeypatch, capsys):
    monkeypatch.delenv(job.GATE_ENV, raising=False)

    async def boom(*a, **k):  # pragma: no cover - reached only by a regression
        raise AssertionError("gate off must not connect to the database")

    monkeypatch.setattr(database, "connect", boom)
    assert job.main([]) == 0
    assert '"gate_enabled": false' in capsys.readouterr().out


def test_main_refuses_a_non_positive_budget():
    with pytest.raises(SystemExit) as exc:
        job.main(["--budget-seconds", "0"])
    assert exc.value.code == 2


# ── what each preflight is asked ────────────────────────────────────────────────────────────


async def test_every_preflight_call_carries_no_buyer_and_the_rows_market_and_variant():
    spy = SpyPreflight({})
    rows = [Merchant("judydoll.com", "US", "50041364447509"), Merchant("robinsons.com.sg", "SG", None),
            Merchant("luafee.jp", "JP", "47000000000002")]
    await run(dry_run=True, merchants=rows, preflight_fn=spy)
    assert len(spy.calls) == 3
    seen = {}
    for host, kwargs in spy.calls:
        assert "buyer" in kwargs and kwargs["buyer"] is None, "a buyer reached the preflight"
        assert kwargs["client"] is not None
        assert kwargs["click_id"].startswith("clk_tierbelig_T_")
        assert kwargs["product_handle"] is None and kwargs["quantity"] == 1
        seen[host] = (kwargs["market"], kwargs["variant_id"])
    assert seen == {"judydoll.com": ("US", "50041364447509"), "robinsons.com.sg": ("SG", None),
                    "luafee.jp": ("JP", "47000000000002")}


async def test_the_real_preflight_sends_no_prefill_parameters():
    clock = FakeClock()
    store = Storefronts(clock, {"judydoll.com": "eligible"})
    summary = await run(clock=clock, dry_run=True, merchants=merchants("judydoll.com"), transport=store.transport())
    assert summary.counts == {"ELIGIBLE": 1}
    cart = [u for u in store.urls() if "/cart/" in u]
    assert len(cart) == 1
    assert "checkout%5B" not in cart[0] and "checkout[" not in cart[0]
    assert "pivota_click_id" in cart[0]


# ── retries ─────────────────────────────────────────────────────────────────────────────────


async def test_a_retryable_result_is_retried_exactly_once():
    spy = SpyPreflight({"judydoll.com": ["TRANSPORT_ERROR", "ELIGIBLE"], "podl.us": ["TRANSPORT_ERROR"] * 5})
    clock = FakeClock()
    summary = await run(clock=clock, dry_run=True, merchants=merchants("judydoll.com", "podl.us"), preflight_fn=spy,
                        retry_delay_s=2.0)
    by_host = {o.merchant.domain: o for o in summary.outcomes}
    assert by_host["judydoll.com"].attempts == 2
    assert by_host["judydoll.com"].result.verdict is Verdict.ELIGIBLE
    assert by_host["podl.us"].attempts == 2  # not three
    assert by_host["podl.us"].result.verdict is Verdict.TRANSPORT_ERROR
    assert [h for h, _ in spy.calls].count("podl.us") == 2
    assert 2.0 in clock.sleeps
    assert summary.exit_code == job.EXIT_INDEFINITE


@pytest.mark.parametrize("verdict", ["UNCLASSIFIED", "VARIANT_UNVERIFIED", "INVALID_INPUT", "ELIGIBLE", "LOGIN_REQUIRED"])
async def test_a_non_retryable_result_is_not_retried(verdict):
    spy = SpyPreflight({"judydoll.com": [verdict, "ELIGIBLE"]})
    summary = await run(dry_run=True, merchants=merchants("judydoll.com"), preflight_fn=spy)
    assert len(spy.calls) == 1
    assert summary.outcomes[0].attempts == 1
    assert summary.outcomes[0].result.verdict.value == verdict


# ── pacing ──────────────────────────────────────────────────────────────────────────────────


async def test_the_pacer_spaces_request_starts_at_least_1_5_s_apart():
    clock = FakeClock()
    pacer = job.RequestPacer(1.5, clock=clock, sleep=clock.sleep)
    await asyncio.gather(*(pacer.acquire() for _ in range(8)))
    assert len(pacer.starts) == 8
    assert pacer.starts[0] == 1000.0  # the first request does not wait
    gaps = [b - a for a, b in zip(pacer.starts, pacer.starts[1:])]
    assert all(g >= 1.5 for g in gaps), gaps
    assert max(gaps) < 1.5 + 1e-9  # and no slower than it has to be


async def test_the_pacer_interval_cannot_be_lowered_below_the_floor():
    clock = FakeClock()
    pacer = job.RequestPacer(0.1, clock=clock, sleep=clock.sleep)
    assert pacer.min_interval_s == job.MIN_REQUEST_INTERVAL_S == 1.5
    await asyncio.gather(*(pacer.acquire() for _ in range(3)))
    assert pacer.starts == [1000.0, 1001.5, 1003.0]


async def test_a_request_after_a_natural_gap_does_not_wait():
    clock = FakeClock()
    pacer = job.RequestPacer(clock=clock, sleep=clock.sleep)
    await pacer.acquire()
    clock.now += 10
    await pacer.acquire()
    assert pacer.starts == [1000.0, 1010.0]
    assert clock.sleeps == []


async def test_every_request_the_job_makes_is_paced_across_merchants():
    """The limiter is GLOBAL and wraps the transport: measured at the storefront, every request
    start — catalog reads and redirect hops, on every host — is >= 1.5 s after the previous."""
    clock = FakeClock()
    kinds = {"judydoll.com": "eligible", "podl.us": "login", "luafee.jp": "not_accepting",
             "extra1.us": "eligible", "extra2.us": "eligible"}
    store = Storefronts(clock, kinds)
    summary = await run(clock=clock, dry_run=True, merchants=merchants(*kinds), transport=store.transport())
    times = store.times()
    assert summary.counts == {"ELIGIBLE": 3, "LOGIN_REQUIRED": 1, "NOT_ACCEPTING_ORDERS": 1}
    assert len(times) == 3 * len(kinds)  # catalog page, permalink, one redirect hop — per store
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 1.5 - 1e-9 for g in gaps), gaps
    assert len({r.url.host for _, r in store.requests}) >= 5
    assert summary.requests == len(times)


async def test_at_most_three_merchants_are_in_flight():
    spy = SpyPreflight({})
    rows = [Merchant(f"m{i}.us", "US", None) for i in range(10)]
    summary = await run(dry_run=True, merchants=rows, preflight_fn=spy, concurrency=50)
    assert len(spy.calls) == 10
    assert spy.max_in_flight == 3
    assert summary.checked == 10


async def test_a_lower_concurrency_is_honoured():
    spy = SpyPreflight({})
    rows = [Merchant(f"m{i}.us", "US", None) for i in range(5)]
    await run(dry_run=True, merchants=rows, preflight_fn=spy, concurrency=1)
    assert spy.max_in_flight == 1


# ── the budget ──────────────────────────────────────────────────────────────────────────────


async def test_the_budget_stops_new_requests_and_reports_the_rest_budget_stopped():
    clock = FakeClock()
    kinds = {h: "eligible" for h in ("judydoll.com", "extra1.us", "extra2.us", "extra3.us", "extra4.us")}
    store = Storefronts(clock, kinds)
    rec = SpyRecorder()
    summary = await run(clock=clock, dry_run=False, record_fn=rec, merchants=merchants(*kinds),
                        transport=store.transport(), budget_s=10)
    assert all(t < 1000.0 + 10 for t in store.times()), store.times()
    assert len(store.times()) == 7  # starts at 0, 1.5, ... 9.0
    assert summary.budget_stopped >= 1
    assert summary.exit_code == job.EXIT_BUDGET
    stopped = [o for o in summary.outcomes if o.status == "budget_stopped"]
    # Cut short mid-preflight or never begun: either way no result and no usable attempt.
    assert stopped and all(o.attempts == 0 and o.result is None for o in stopped)
    budget_stopped = {o.merchant.domain for o in stopped}
    assert not (budget_stopped & {d for d, _, _ in rec.calls})
    assert summary.counts.get("BUDGET_STOPPED") == summary.budget_stopped


async def test_a_budget_hit_during_the_retry_keeps_the_first_result():
    clock = FakeClock()

    async def slow_flake(host, **kwargs):
        clock.now += 30
        return result("TRANSPORT_ERROR", host=host)

    summary = await run(clock=clock, dry_run=True, merchants=merchants("judydoll.com"), preflight_fn=slow_flake,
                        budget_s=20)
    outcome = summary.outcomes[0]
    assert outcome.attempts == 1 and outcome.result.verdict is Verdict.TRANSPORT_ERROR
    assert summary.exit_code == job.EXIT_INDEFINITE


# ── recording, dry-run, failures ────────────────────────────────────────────────────────────


async def test_a_dry_run_writes_nothing(db):
    clock = FakeClock()
    kinds = {"judydoll.com": "eligible", "podl.us": "login"}
    store = Storefronts(clock, kinds)
    rec = SpyRecorder()
    lines: List[str] = []
    summary = await run(clock=clock, dry_run=True, record_fn=rec, merchants=merchants(*kinds),
                        transport=store.transport(), emit=lines.append)
    assert summary.counts == {"ELIGIBLE": 1, "LOGIN_REQUIRED": 1}
    assert rec.calls == []
    row = await database.fetch_one(f"SELECT COUNT(*) AS n FROM {TABLE}")
    assert int(row["n"]) == 0
    assert len(lines) == 2 and all("recorded=-" in line for line in lines)


async def test_end_to_end_records_definite_verdicts_and_keeps_a_prior_one_on_a_transport_error(db):
    await elig.record_result("flaky.us", "US", result("ELIGIBLE", host="flaky.us"))
    clock = FakeClock()
    await elig.record_result("extra1.us", "US", result("ELIGIBLE", host="extra1.us"))
    kinds = {"judydoll.com": "eligible", "podl.us": "login", "luafee.jp": "not_accepting", "flaky.us": "transport",
             "extra1.us": "mismatch"}
    store = Storefronts(clock, kinds)
    rows = merchants("judydoll.com", "podl.us", "flaky.us", "extra1.us") + [Merchant("luafee.jp", "JP", VIDS["luafee.jp"])]
    summary = await run(clock=clock, dry_run=False, merchants=rows, transport=store.transport())

    assert summary.counts == {"ELIGIBLE": 1, "LOGIN_REQUIRED": 1, "NOT_ACCEPTING_ORDERS": 1, "TRANSPORT_ERROR": 1,
                              "CHECKOUT_MARKET_MISMATCH": 1}
    assert summary.definite == 4 and summary.indefinite == 1 and summary.record_failures == 0
    assert summary.exit_code == job.EXIT_OK  # 1 of 5 indefinite: a flaky store, not an alarm
    judy = await elig.get_eligibility("judydoll.com", "US")
    assert judy["verdict"] == "ELIGIBLE" and judy["checkout_country"] == "US"
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is True
    # A checkout that landed in JP for a US buyer is a definite NO, and it replaces ELIGIBLE.
    moved = await elig.get_eligibility("extra1.us", "US")
    assert moved["verdict"] == "CHECKOUT_MARKET_MISMATCH" and moved["previous_verdict"] == "ELIGIBLE"
    assert moved["checkout_country"] == "JP"
    assert await elig.is_cart_link_eligible("extra1.us", "US") is False
    assert (await elig.get_eligibility("podl.us", "US"))["verdict"] == "LOGIN_REQUIRED"
    assert (await elig.get_eligibility("luafee.jp", "JP"))["verdict"] == "NOT_ACCEPTING_ORDERS"
    flaky = await elig.get_eligibility("flaky.us", "US")
    assert flaky["verdict"] == "ELIGIBLE", "a transport error overwrote a prior verdict"
    assert flaky["last_error_code"].startswith("TRANSPORT_ERROR:")
    # the flaky store was tried twice (one retry), and never a third time
    flaky_attempts = [u for u in store.urls() if "flaky.us/products.json" in u]
    assert len(flaky_attempts) == 2


async def test_a_record_failure_exits_non_zero_and_does_not_stop_the_run():
    spy = SpyPreflight({})
    rec = SpyRecorder(fail=True)
    summary = await run(dry_run=False, record_fn=rec, merchants=merchants("judydoll.com", "podl.us"), preflight_fn=spy)
    assert len(rec.calls) == 2
    assert summary.record_failures == 2
    assert summary.exit_code == job.EXIT_RECORD_FAILED


async def test_a_crashing_preflight_is_counted_and_the_rest_still_run():
    async def crashy(host, **kwargs):
        if host == "podl.us":
            raise RuntimeError("bug")
        return result("ELIGIBLE", host=host)

    rec = SpyRecorder()
    summary = await run(dry_run=False, record_fn=rec, merchants=merchants("judydoll.com", "podl.us"), preflight_fn=crashy)
    assert summary.crashed == 1 and summary.checked == 1
    assert [d for d, _, _ in rec.calls] == ["judydoll.com"]
    assert summary.exit_code == job.EXIT_RECORD_FAILED


async def test_a_bad_merchant_list_attempts_nothing_and_exits_2(monkeypatch):
    def bad():
        raise MerchantListError("row 3: duplicate merchant")

    monkeypatch.setattr(job, "load_merchants", bad)
    spy = SpyPreflight({})
    summary = await run(dry_run=True, preflight_fn=spy)
    assert summary.exit_code == job.EXIT_BAD_MERCHANT_LIST and spy.calls == []
    summary = await run(dry_run=True, preflight_fn=spy, merchants=merchants("judydoll.com"), only=["other.com"])
    assert summary.exit_code == job.EXIT_BAD_MERCHANT_LIST and spy.calls == []


async def test_all_definite_exits_zero():
    summary = await run(dry_run=True, merchants=merchants("judydoll.com", "podl.us"),
                        preflight_fn=SpyPreflight({"podl.us": ["LOGIN_REQUIRED"]}))
    assert summary.exit_code == job.EXIT_OK
    assert summary.counts == {"ELIGIBLE": 1, "LOGIN_REQUIRED": 1}


def test_the_indefinite_alarm_fires_only_above_a_quarter():
    s = job.RunSummary(gate_enabled=True, dry_run=False, checked=4, definite=3, indefinite=1)
    assert job._exit_code(s) == 0
    s.definite, s.indefinite = 2, 2
    assert job._exit_code(s) == 1
    s = job.RunSummary(gate_enabled=True, dry_run=False, checked=40, definite=30, indefinite=10)
    assert job._exit_code(s) == 0
    s.definite, s.indefinite = 29, 11
    assert job._exit_code(s) == 1
    assert job._exit_code(job.RunSummary(gate_enabled=True, dry_run=False)) == 0  # nothing checked


async def test_each_indefinite_merchant_is_listed_at_warning(caplog):
    spy = SpyPreflight({"podl.us": ["UNCLASSIFIED"]})
    with caplog.at_level(logging.WARNING, logger=job.__name__):
        await run(dry_run=True, merchants=merchants("judydoll.com", "podl.us"), preflight_fn=spy)
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("podl.us" in line and "UNCLASSIFIED" in line for line in lines)
    assert not any("judydoll.com" in line for line in lines)


def test_exit_code_precedence():
    s = job.RunSummary(gate_enabled=True, dry_run=False)
    assert job._exit_code(s) == 0
    s.checked, s.indefinite = 1, 1
    assert job._exit_code(s) == 1
    s.budget_stopped = 1
    assert job._exit_code(s) == 3
    s.record_failures = 1
    assert job._exit_code(s) == 4
    s.record_failures, s.crashed = 0, 1
    assert job._exit_code(s) == 4
    s.error = "bad list"
    assert job._exit_code(s) == 2


# ── logging ─────────────────────────────────────────────────────────────────────────────────


def test_the_printed_landing_is_redacted():
    leaky = "https://judydoll.com/checkouts/cn/T/information?checkout%5Bemail%5D=a%40b.test&attributes%5Bpivota_click_id%5D=clk_x"
    line = job.outcome_line(job.MerchantOutcome(
        merchant=Merchant("judydoll.com", "US", None),
        result=PreflightResult(host="judydoll.com", verdict=Verdict.ELIGIBLE, market="US", final_url=leaky),
        attempts=1, status="checked",
    ))
    assert "a%40b.test" not in line and "a@b.test" not in line
    assert "REDACTED" in line and "judydoll.com/checkouts/cn/T/information" in line


async def test_no_log_record_or_printed_line_carries_a_prefill_or_unredacted_query(caplog):
    clock = FakeClock()
    kinds = {"judydoll.com": "eligible", "podl.us": "login"}
    store = Storefronts(clock, kinds)
    lines: List[str] = []
    with caplog.at_level(logging.DEBUG):
        await run(clock=clock, dry_run=True, merchants=merchants(*kinds), transport=store.transport(),
                  emit=lines.append)
    text = "\n".join([r.getMessage() for r in caplog.records] + lines)
    assert "checkout%5B" not in text and "checkout[" not in text


def test_the_default_transport_honours_an_https_proxy_only_when_one_is_set(monkeypatch):
    import httpcore

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    direct = job._default_inner_transport()
    assert not isinstance(direct._pool, httpcore.AsyncHTTPProxy)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    proxied = job._default_inner_transport()
    assert isinstance(proxied._pool, httpcore.AsyncHTTPProxy)
