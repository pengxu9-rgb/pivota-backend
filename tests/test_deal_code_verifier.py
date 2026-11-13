"""services/deal_code_verifier.py: the crawled-code verifier (ADR-027 D3, Q17-Q20; coupon discovery design §3.3).

Pinned: it is off unless both flags and a full-length key are set, and then reads nothing; which crawl rows are due and
in what order; the product a code is tested on; the per-run, per-host and daily caps (the daily slot is reserved
before the call); what each answer from the gateway route writes, and what it never writes; a refused code is never
tested again, even when the crawl writes it again; nothing raises; logs and counts never carry a code or a host.
Postgres twin: tests/test_deal_code_verifier_postgres.py."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import deal_code_verifier as dv  # noqa: E402

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
KEY = "k" * 43
_SEED_COLUMNS = ("id", "market", "domain", "destination_url", "canonical_url", "price_currency", "availability",
                 "seed_data", "status")


def _on(monkeypatch, **extra):
    monkeypatch.setenv("DEAL_VERIFY_JOB_ENABLED", "true")
    monkeypatch.setenv("DEAL_VERIFY_NONCONNECTED_ENABLED", "true")
    monkeypatch.setenv("DEAL_VERIFY_INTERNAL_KEY", KEY)
    monkeypatch.setenv("DEAL_VERIFY_ROUTE_URL", "https://gateway.test/internal/ucp/discount-code/verify")
    for k, val in extra.items():
        monkeypatch.setenv(k, str(val))


async def _ensure_seed_table(database) -> bool:
    """The seed columns this file reads, on the shared test database. True when this file CREATED the table, so the
    fixture drops it again: a narrow table left behind would break every later file that creates its own wider one
    with CREATE TABLE IF NOT EXISTS (the sweep shards share one SQLite file). An existing table only gains columns."""
    rows = await database.fetch_all("PRAGMA table_info(external_product_seeds)")
    have = {r["name"] for r in rows}
    created = not have
    if created:
        await database.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY)")
        have = {"id"}
    try:
        for col in _SEED_COLUMNS:
            if col not in have:
                await database.execute(f"ALTER TABLE external_product_seeds ADD COLUMN {col} TEXT")
    except BaseException:
        if created:  # never leave a narrow table this file made (review N-4 residual)
            await database.execute("DROP TABLE IF EXISTS external_product_seeds")
        raise
    return created


@pytest.fixture
async def db(monkeypatch):
    from db import deal_lane
    from db.catalog import catalog_products
    from db.database import database, engine, metadata

    tables = [deal_lane.observed_store_offers, deal_lane.observed_store_offer_verifications, deal_lane.deal_lane_optout,
              deal_lane.deal_verify_host_daily_carts, deal_lane.deal_code_refusals, catalog_products]
    metadata.create_all(engine, tables=tables)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    created_seed_table = await _ensure_seed_table(database)

    async def _clean():
        for table in reversed(tables):
            await database.execute(table.delete())
        await database.execute("DELETE FROM external_product_seeds WHERE id LIKE 'dcv-%'")

    try:
        # Setup is inside the try, so a failure here still drops a table this file created (review N-4).
        await _clean()
        for name in ("DEAL_VERIFY_MAX_CARTS_PER_RUN", "DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY",
                     "DEAL_VERIFY_TTL_HOURS"):
            monkeypatch.delenv(name, raising=False)
        from services import deal_code_capture

        deal_code_capture.reset_stats()
        yield database
    finally:
        await _clean()
        if created_seed_table:
            await database.execute("DROP TABLE IF EXISTS external_product_seeds")
        if not was_connected:
            await database.disconnect()


async def offer(db, name, *, seller="glow.example", market="US", code=None, kind="percent", value=15, currency=None,
                min_subtotal=None, observed=NOW - timedelta(hours=1), ends_at=None, crawl=True):
    from db.deal_lane import observed_store_offer_key, observed_store_offers as o

    deal_id = ("dlc_" if crawl else "dl_") + name
    code = code or ("SAVE" + name.upper() + "1")
    await db.execute(o.insert().values(
        deal_id=deal_id, deal_key=observed_store_offer_key(seller_id=seller, market=market, code=code,
                                                           platform_rule_ref=None, program_id=None),
        seller_id=seller, market=market, code=code, kind=kind, value=value, currency=currency,
        min_subtotal=min_subtotal, source="merchant_published", observed_at=observed, ends_at=ends_at,
        created_at=observed, updated_at=observed))
    return deal_id


async def seed(db, sid, *, host="glow.example", variants=None, currency="USD", status="active",
               www=False, availability="in_stock", partition="US"):
    """A seed shaped as the real writer writes it: the `market` column is the storage partition, 'US' for every
    seed (ingestion.SEED_PARTITION_MARKET), SGD ones included; the seed's market is its currency's."""
    variants = variants if variants is not None else [
        {"shopify_variant_id": "44000000000001", "price_amount": 30, "price_currency": currency,
         "availability": "in_stock"}]
    url = f"https://{'www.' if www else ''}{host}/products/{sid}"
    await db.execute(
        "INSERT INTO external_product_seeds (id, market, domain, destination_url, canonical_url, price_currency,"
        " availability, seed_data, status) VALUES (:id, :m, :d, :u, :u, :c, :a, :sd, :s)",
        {"id": "dcv-" + sid, "m": partition, "d": ("www." if www else "") + host, "u": url, "c": currency,
         "a": availability, "sd": json.dumps({"snapshot": {"variants": variants}}), "s": status})


async def verifications(db, deal_id=None):
    from db.deal_lane import observed_store_offer_verifications as v

    q = v.select() if deal_id is None else v.select().where(v.c.deal_id == deal_id)
    return [dict(r) for r in await db.fetch_all(q)]


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class StubClient:
    """Answers each POST from `answers` (a list, or one answer for every call) and records the calls."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        answer = self.answers.pop(0) if isinstance(self.answers, list) else self.answers
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return _Resp(status, body)


APPLIED = (200, {"result": "applied", "reason": None, "currency": "USD",
                 "totals": {"subtotal": 3000, "discount": 450, "total": 2550}, "unit_price": 3000,
                 "discount": {"amount": 450}, "automatic": []})
REJECTED = (200, {"result": "rejected", "reason": "discount_code_invalid"})
NOT_PRICED = (200, {"result": "not_priced", "reason": "timeout"})


async def _nosleep(_s):
    return None


async def run(client, now=NOW):
    return await dv.run(now=now, client=client, sleep=_nosleep)


# ---- gates -----------------------------------------------------------------------------------------------------------

_URL = "https://gateway.test/internal/ucp/discount-code/verify"


@pytest.mark.parametrize("job,nonconnected,key,url,on", [
    ("true", "true", KEY, _URL, True),
    ("", "true", KEY, _URL, False),
    ("true", "", KEY, _URL, False),
    ("true", "true", "k" * 42, _URL, False),
    ("true", "true", "", _URL, False),
    ("1", "true", KEY, _URL, False),
    ("true", "true", KEY, "", False),  # no default URL (review P3-9)
    ("true", "true", KEY, "http://gateway.test/internal/ucp/discount-code/verify", False),
])
async def test_it_runs_only_with_both_flags_a_full_length_key_and_an_https_url(monkeypatch, job, nonconnected, key,
                                                                               url, on):
    monkeypatch.setenv("DEAL_VERIFY_JOB_ENABLED", job)
    monkeypatch.setenv("DEAL_VERIFY_NONCONNECTED_ENABLED", nonconnected)
    monkeypatch.setenv("DEAL_VERIFY_INTERNAL_KEY", key)
    monkeypatch.setenv("DEAL_VERIFY_ROUTE_URL", url)
    assert dv.enabled() is on


def test_the_route_url_has_no_default(monkeypatch):
    from config.settings import settings

    monkeypatch.delenv("DEAL_VERIFY_ROUTE_URL", raising=False)
    assert settings.deal_verify_route_url == ""


def test_only_markets_whose_currency_minor_unit_is_known_are_verified():
    assert dv.MARKET_CURRENCY == {"US": "USD", "SG": "SGD"}
    assert all(c in dv.MINOR_EXPONENT for c in dv.MARKET_CURRENCY.values())


async def test_off_it_reads_nothing_and_returns_nothing(monkeypatch):
    from db.database import database

    for name in ("DEAL_VERIFY_JOB_ENABLED", "DEAL_VERIFY_NONCONNECTED_ENABLED", "DEAL_VERIFY_INTERNAL_KEY"):
        monkeypatch.delenv(name, raising=False)

    async def boom(*a, **k):
        raise AssertionError("read while off")

    monkeypatch.setattr(database, "fetch_all", boom)
    monkeypatch.setattr(database, "fetch_one", boom)
    client = StubClient([])
    assert await dv.run(now=NOW, client=client) == {}
    assert client.calls == []


# ---- due ------------------------------------------------------------------------------------------------------------

def _o(**kw):
    base = {"kind": "percent", "market": "US", "code": "SAVE15", "currency": None, "ends_at": None, "starts_at": None,
            "observed_at": NOW - timedelta(hours=1)}
    base.update(kw)
    return base


def _ver(state, *, hours_ago=1, expires_in=None, reason=None):
    return {"state": state, "updated_at": NOW - timedelta(hours=hours_ago),
            "expires_at": None if expires_in is None else NOW + timedelta(hours=expires_in), "failure_reason": reason}


@pytest.mark.parametrize("offer_kw,rows,expected", [
    ({}, [], 0),
    ({"kind": "free_shipping"}, [], None),
    ({"kind": "gift"}, [], None),
    ({"kind": "amount", "currency": "USD"}, [], 0),
    ({"kind": "amount", "currency": "SGD"}, [], None),  # another currency than the market's
    ({"market": None}, [], None),
    ({"market": "GB"}, [], None),
    ({"code": " "}, [], None),
    ({"ends_at": NOW}, [], None),
    ({"starts_at": NOW + timedelta(hours=1)}, [], None),
    ({}, [_ver("failed", hours_ago=24 * 30)], None),  # a refusal is never retried
    ({}, [_ver("checkout_verified", expires_in=4)], None),
    ({}, [_ver("checkout_verified", hours_ago=21, expires_in=2)], 1),
    ({}, [_ver("checkout_verified", hours_ago=1, expires_in=2, reason="not_priced:timeout")], None),
    ({}, [_ver("checkout_verified", hours_ago=13, expires_in=2, reason="not_priced:timeout")], 1),
    ({}, [_ver("checkout_verified", hours_ago=30, expires_in=-6)], 1),  # lapsed: re-check
    ({}, [_ver("unverified", hours_ago=11, reason="not_priced:timeout")], None),
    ({}, [_ver("unverified", hours_ago=13, reason="not_priced:timeout")], 2),
    ({}, [_ver("unverified", hours_ago=24 * 6, reason="not_evaluated:seller_does_not_advertise")], None),
    ({}, [_ver("unverified", hours_ago=24 * 7 + 1, reason="not_evaluated:seller_does_not_advertise")], 2),
    ({}, [_ver("unverified", hours_ago=24 * 6, reason="route:seller_host_refused")], None),  # review P2-1
    ({}, [_ver("unverified", hours_ago=24 * 7 + 1, reason="route:transport_readtimeout")], 2),
])
def test_which_rows_are_due(offer_kw, rows, expected):
    got = dv.due(_o(**offer_kw), rows, NOW)
    assert (got[0] if got else None) == expected


# ---- the reference variant -----------------------------------------------------------------------------------------

def _seed(host="glow.example", variants=(), currency="USD", www=False, availability=None):
    url = f"https://{'www.' if www else ''}{host}/products/p"
    return {"market": "US", "canonical_url": url, "destination_url": url, "price_currency": currency,
            "availability": availability,
            "seed_data": {"snapshot": {"variants": list(variants)}}}


def _var(vid, price, currency="USD", availability="in_stock"):
    return {"shopify_variant_id": vid, "price_amount": price, "price_currency": currency, "availability": availability}


def test_the_reference_is_the_cheapest_in_stock_store_variant_in_the_markets_currency():
    seeds = [
        _seed(variants=[_var("44000000000005", 40), _var("44000000000004", 12, availability="out_of_stock")]),
        _seed(variants=[_var("44000000000003", 25), _var("44000000000002", 9, currency="SGD"),
                        _var("not-a-number", 5), _var("44000000000009", None)]),
        _seed(host="other.example", variants=[_var("44000000000001", 1)]),
    ]
    ref, why = dv.pick_reference_variant(seeds, seller="glow.example", market="US", min_subtotal=None)
    assert (ref.variant_id, ref.unit_price, ref.discovery_host, why) == ("44000000000003", Decimal("25"), None, "")


def test_a_minimum_spend_picks_a_variant_at_or_above_it_or_says_why_not():
    seeds = [_seed(variants=[_var("44000000000003", 25), _var("44000000000004", 60), _var("44000000000005", 75)])]
    ref, _ = dv.pick_reference_variant(seeds, seller="glow.example", market="US", min_subtotal=Decimal("60"))
    assert ref.variant_id == "44000000000004"
    ref, why = dv.pick_reference_variant(seeds, seller="glow.example", market="US", min_subtotal=Decimal("80"))
    assert ref is None and why == "min_subtotal_unmet"
    ref, why = dv.pick_reference_variant([], seller="glow.example", market="US", min_subtotal=None)
    assert ref is None and why == "no_reference_variant"


@pytest.mark.parametrize("order", [1, -1])
def test_ties_go_to_the_lowest_store_id_numerically_and_www_hosts_are_discovered_as_the_seed_names_them(order):
    seeds = [_seed(www=True, variants=[_var("440000000000020", 25), _var("44000000000003", 25)][::order])]
    ref, _ = dv.pick_reference_variant(seeds, seller="glow.example", market="US", min_subtotal=None)
    assert ref.variant_id == "44000000000003"  # 14 digits before 15, not string order
    assert ref.discovery_host == "www.glow.example"


def test_an_sgd_seed_in_the_us_partition_serves_sg_and_never_us():
    """Every seed is stored in partition 'US' (review P1-1): its market is its own currency's."""
    sgd = _seed(currency="SGD", variants=[_var("44000000000003", 40, currency="SGD")])
    usd = _seed(variants=[_var("44000000000004", 30)])
    ref, _ = dv.pick_reference_variant([sgd, usd], seller="glow.example", market="SG", min_subtotal=None)
    assert ref.variant_id == "44000000000003"
    ref, _ = dv.pick_reference_variant([sgd, usd], seller="glow.example", market="US", min_subtotal=None)
    assert ref.variant_id == "44000000000004"


def test_a_seed_whose_column_and_page_currency_disagree_serves_no_market():
    seed = _seed(currency="SGD", variants=[_var("44000000000003", 40, currency="SGD")])
    seed["seed_data"]["snapshot"]["price_currency"] = "USD"
    for market in ("SG", "US"):
        ref, why = dv.pick_reference_variant([seed], seller="glow.example", market=market, min_subtotal=None)
        assert ref is None and why == "no_reference_variant"


def test_a_variant_without_its_own_stock_takes_the_seeds_and_without_either_is_not_used():
    v = {"shopify_variant_id": "44000000000003", "price_amount": 25, "price_currency": "USD"}
    ref, _ = dv.pick_reference_variant([_seed(variants=[v], availability="in_stock")], seller="glow.example",
                                       market="US", min_subtotal=None)
    assert ref is not None
    ref, why = dv.pick_reference_variant([_seed(variants=[v], availability=None)], seller="glow.example",
                                         market="US", min_subtotal=None)
    assert ref is None and why == "no_reference_variant"


# ---- the answer ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("amount,currency,expected", [
    (450, "USD", Decimal("4.50")), (0, "USD", Decimal("0.00")), (199, "SGD", Decimal("1.99")),
    (4.5, "USD", None), ("450", "USD", None), (-1, "USD", None), (True, "USD", None), (450, "JPY", None),
    (None, "USD", None),
])
def test_minor_units_become_major_units_only_for_an_integer_in_a_known_currency(amount, currency, expected):
    assert dv.from_minor(amount, currency) == expected


@pytest.mark.parametrize("status,body,result,reason", [
    (200, {"result": "applied"}, "applied", ""),
    (200, {"result": "rejected", "reason": "discount_code_invalid"}, "rejected", "discount_code_invalid"),
    (200, {"result": "invalid", "reason": "code_invalid"}, "refused_request", "code_invalid"),
    (400, {"error": "invalid_request", "reason": "seller_host_refused"}, "refused_request", "seller_host_refused"),
    (400, "not json", "refused_request", "invalid_request"),
    (200, {"result": "surprise"}, "unreadable_answer", "unknown_result"),  # review N-1
    (200, ["applied"], "unreadable_answer", "unreadable"),
    (200, None, "unreadable_answer", "unreadable"),
    (401, {"error": "unauthorized"}, "error", "http_401"),
    (404, {"error": "not_found"}, "error", "http_404"),
    (503, None, "error", "http_503"),
])
def test_what_each_route_answer_reads_as(status, body, result, reason):
    got = dv.read_answer(status, body)
    assert (got.result, got.reason) == (result, reason)


def test_a_reason_that_is_not_a_short_token_is_dropped():
    assert dv.read_answer(200, {"result": "rejected", "reason": "Code SAVE15 is invalid!"}).reason == ""


def _cand(**kw):
    base = dict(deal_id="dlc_a", seller="glow.example", market="US", code="SAVE15", kind="percent", currency=None,
                min_subtotal=None, ends_at=None, priority=0, order_at=NOW)
    base.update(kw)
    return dv.Candidate(**base)


_REF = dv.ReferenceVariant(variant_id="44000000000003", unit_price=Decimal("30"), discovery_host=None)


def test_an_applied_answer_records_the_whole_reference_cart_in_major_units():
    body = dict(APPLIED[1], automatic=[{"amount": 100}])
    values, why = dv.verified_values(dv.Answer("applied", "", body), _cand(), _REF, NOW)
    assert why == ""
    assert values["state"] == "checkout_verified" and values["verified_via"] == "ucp_checkout"
    assert values["authorized_by"] == "q2_approval" and values["ref_quantity"] == 1
    assert (values["ref_unit_price"], values["ref_subtotal"], values["ref_discount_amount"]) == (
        Decimal("30.00"), Decimal("30.00"), Decimal("4.50"))
    assert values["ref_automatic_discounts"] == [{"title": "", "type": "line", "amount": "1.00", "currency": "USD"}]
    assert values["verified_at"] == NOW and values["expires_at"] == NOW + timedelta(hours=24)


def test_the_expiry_is_the_earlier_of_the_deals_end_and_the_ttl(monkeypatch):
    end = NOW + timedelta(hours=5)
    values, _ = dv.verified_values(dv.Answer("applied", "", APPLIED[1]), _cand(ends_at=end), _REF, NOW)
    assert values["expires_at"] == end
    monkeypatch.setenv("DEAL_VERIFY_TTL_HOURS", "2")
    values, _ = dv.verified_values(dv.Answer("applied", "", APPLIED[1]), _cand(ends_at=end), _REF, NOW)
    assert values["expires_at"] == NOW + timedelta(hours=2)


@pytest.mark.parametrize("change,why", [
    ({"currency": "SGD"}, "currency_not_market"),
    ({"unit_price": None}, "applied_incomplete"),
    ({"unit_price": 0}, "applied_incomplete"),
    ({"totals": {}}, "applied_incomplete"),
    ({"discount": None}, "applied_incomplete"),
    ({"discount": {"amount": 0}}, "applied_without_amount"),
    ({"automatic": [{"amount": 1.5}]}, "automatic_unreadable"),
    ({"discount": {"amount": 5000}}, "reference_cart_inconsistent"),  # more than the subtotal (review P3-2)
    ({"totals": {"subtotal": 3500}}, "reference_cart_inconsistent"),  # subtotal above the unit price
    ({"totals": {"subtotal": 2000}}, "reference_cart_inconsistent"),  # subtotal below the effective price
    ({"automatic": [{"amount": 2600}]}, "reference_cart_inconsistent"),  # code + automatic above the unit price
])
def test_an_applied_answer_without_a_whole_cart_in_the_markets_currency_is_not_a_verification(change, why):
    body = dict(APPLIED[1], **change)
    values, got = dv.verified_values(dv.Answer("applied", "", body), _cand(), _REF, NOW)
    assert values is None and got == why


# ---- the run ---------------------------------------------------------------------------------------------------------

async def test_an_applied_code_becomes_checkout_verified_and_the_call_carries_exactly_the_route_fields(db, monkeypatch):
    _on(monkeypatch)
    deal = await offer(db, "a", code="SAVE15X")
    await seed(db, "s1")
    client = StubClient([APPLIED])
    out = await run(client)
    assert out["applied"] == 1 and out["carts"] == 1
    call = client.calls[0]
    assert call["url"] == "https://gateway.test/internal/ucp/discount-code/verify"
    assert call["headers"]["X-Internal-Key"] == KEY
    assert call["json"] == {"seller_host": "glow.example", "variant_gid": "44000000000001", "market": "US",
                            "code": "SAVE15X", "expected_currency": "USD"}
    [row] = await verifications(db, deal)
    assert row["state"] == "checkout_verified" and row["variant_id"] == "44000000000001"
    assert Decimal(str(row["ref_discount_amount"])) == Decimal("4.50")
    # A verified code is not tested again before it nears its expiry.
    again = StubClient([])
    assert (await run(again, now=NOW + timedelta(hours=2))).get("carts") == 0 and again.calls == []


async def test_a_rejected_code_is_failed_hashed_and_never_tested_again_even_when_the_crawl_writes_it_again(db, monkeypatch):
    from db.deal_lane import deal_code_refusals, observed_store_offers as o

    _on(monkeypatch)
    deal = await offer(db, "r", code="DEAD10")
    await seed(db, "s1")
    out = await run(StubClient([REJECTED]))
    assert out["rejected"] == 1
    [row] = await verifications(db, deal)
    assert row["state"] == "failed" and row["failure_reason"] == "rejected:discount_code_invalid"
    assert (row["verified_via"], row["authorized_by"]) == ("ucp_checkout", "q2_approval")  # what allowed the cart
    [h] = await db.fetch_all(deal_code_refusals.select())
    assert h["refusal_hash"] == dv.refusal_hash("glow.example", "US", "dead10") and "DEAD10" not in str(dict(h))
    assert (await run(StubClient([]), now=NOW + timedelta(days=3))).get("carts") == 0
    # Retention deletes the refused row; the crawl reads the code again and writes a new row.
    await db.execute(o.delete().where(o.c.deal_id == deal))
    await offer(db, "r2", code="Dead10", observed=NOW + timedelta(days=8))
    client = StubClient([])
    out = await run(client, now=NOW + timedelta(days=8, hours=1))
    assert out["skipped_refused_recently"] == 1 and client.calls == []
    # After 30 days the hash no longer stops it.
    out = await run(StubClient([REJECTED]), now=NOW + timedelta(days=31))
    assert out["carts"] == 1


async def test_no_answer_is_not_a_verdict_it_is_retried_after_the_backoff(db, monkeypatch):
    _on(monkeypatch)
    deal = await offer(db, "n")
    await seed(db, "s1")
    assert (await run(StubClient([NOT_PRICED])))["not_priced"] == 1
    [row] = await verifications(db, deal)
    assert row["state"] == "unverified" and row["failure_reason"] == "not_priced:timeout"
    assert (row["verified_via"], row["authorized_by"], row["verified_at"]) == ("ucp_checkout", "q2_approval", None)
    assert (await run(StubClient([]), now=NOW + timedelta(hours=11))).get("carts") == 0
    assert (await run(StubClient([APPLIED]), now=NOW + timedelta(hours=13)))["applied"] == 1


async def test_a_verified_code_that_gets_no_answer_on_re_check_keeps_its_verification_and_amounts(db, monkeypatch):
    _on(monkeypatch)
    deal = await offer(db, "k")
    await seed(db, "s1")
    await run(StubClient([APPLIED]))
    later = NOW + timedelta(hours=22)
    assert (await run(StubClient([NOT_PRICED]), now=later))["not_priced"] == 1
    [row] = await verifications(db, deal)
    assert row["state"] == "checkout_verified" and row["failure_reason"] == "not_priced:timeout"
    assert row["verified_at"] is not None and Decimal(str(row["ref_discount_amount"])) == Decimal("4.50")
    # A rejection on re-check fails it, and still keeps the earlier reference amounts (retention's contract).
    assert (await run(StubClient([REJECTED]), now=later + timedelta(hours=13)))["rejected"] == 1
    [row] = await verifications(db, deal)
    assert row["state"] == "failed" and row["verified_at"] is not None and row["ref_discount_amount"] is not None


@pytest.mark.parametrize("answer,reason", [((401, {"error": "unauthorized"}), "http_401"),
                                           ((404, {"error": "not_found"}), "http_404"),
                                           ((502, None), "http_502")])
async def test_a_route_that_is_down_or_misconfigured_stops_the_run_writes_nothing_and_spends_the_slot(
        db, monkeypatch, answer, reason):
    from db.deal_lane import deal_verify_host_daily_carts as caps

    _on(monkeypatch)
    deal = await offer(db, "e", observed=NOW - timedelta(hours=1))
    other = await offer(db, "f", seller="shop.example", observed=NOW - timedelta(hours=2))
    await seed(db, "s1")
    await seed(db, "s2", host="shop.example")
    client = StubClient([answer])
    out = await run(client)
    assert out["route_errors"] == 1 and out["route_" + reason] == 1 and out["left_route_stopped"] == 1
    assert len(client.calls) == 1  # the next host gets no cart: the fault is the route's (review P2-1)
    assert await verifications(db, deal) == [] and await verifications(db, other) == []
    [cap] = await db.fetch_all(caps.select())
    assert (cap["host"], cap["carts"]) == ("glow.example", 1)


async def test_a_request_the_route_refuses_backs_that_row_off_and_the_run_goes_on(db, monkeypatch):
    _on(monkeypatch)
    bad = await offer(db, "b", observed=NOW - timedelta(hours=1))
    good = await offer(db, "g", seller="shop.example", observed=NOW - timedelta(hours=2))
    await seed(db, "s1")
    await seed(db, "s2", host="shop.example")
    client = StubClient([(400, {"error": "invalid_request", "reason": "seller_host_refused"}), APPLIED])
    out = await run(client)
    assert out["route_seller_host_refused"] == 1 and out["applied"] == 1
    [row] = await verifications(db, bad)
    assert row["state"] == "unverified" and row["failure_reason"] == "route:seller_host_refused"
    assert (await verifications(db, good))[0]["state"] == "checkout_verified"
    # Backed off 7 days: it no longer takes a slot from the codes behind it on the next runs.
    for days in (1, 3, 6):
        client = StubClient(APPLIED)  # the good code is re-verified as it nears its expiry
        await run(client, now=NOW + timedelta(days=days))
        assert "glow.example" not in [c["json"]["seller_host"] for c in client.calls]
    client = StubClient(NOT_PRICED)
    await run(client, now=NOW + timedelta(days=7, hours=1))
    assert "glow.example" in [c["json"]["seller_host"] for c in client.calls]


async def test_a_lost_answer_is_recorded_on_the_row_backed_off_and_stops_the_run(db, monkeypatch):
    _on(monkeypatch)
    lost = await offer(db, "t", observed=NOW - timedelta(hours=1))
    await offer(db, "u", seller="shop.example", observed=NOW - timedelta(hours=2))
    await seed(db, "s1")
    await seed(db, "s2", host="shop.example")
    client = StubClient([RuntimeError("connection reset")])
    out = await run(client)
    assert out["route_errors"] == 1 and out["route_transport_runtimeerror"] == 1 and out["left_route_stopped"] == 1
    assert len(client.calls) == 1
    [row] = await verifications(db, lost)
    assert row["state"] == "unverified" and row["failure_reason"] == "route:transport_runtimeerror"
    assert dv.due({"kind": "percent", "market": "US", "code": "X"}, [dict(row)], NOW + timedelta(days=6)) is None


@pytest.mark.parametrize("answer,reason", [((200, {"result": "surprise"}), "unknown_result"),
                                           ((200, ValueError("not json")), "unreadable")])
async def test_a_200_this_verifier_cannot_read_is_recorded_backed_off_and_stops_the_run(db, monkeypatch, answer,
                                                                                        reason):
    """Review N-1: the gateway answered, so a cart was likely built. Without a row the code would stay first in line
    and cost one cart a day, verifying nothing else."""
    _on(monkeypatch)
    first = await offer(db, "v", observed=NOW - timedelta(hours=1))
    await offer(db, "w", seller="shop.example", observed=NOW - timedelta(hours=2))
    await seed(db, "s1")
    await seed(db, "s2", host="shop.example")
    client = StubClient([answer])
    out = await run(client)
    assert out["route_errors"] == 1 and out["route_" + reason] == 1 and out["left_route_stopped"] == 1
    assert len(client.calls) == 1
    [row] = await verifications(db, first)
    assert row["state"] == "unverified" and row["failure_reason"] == "route:" + reason
    client = StubClient(APPLIED)
    await run(client, now=NOW + timedelta(days=6))
    assert "glow.example" not in [c["json"]["seller_host"] for c in client.calls]


@pytest.mark.parametrize("answer,reason", [
    ((200, {"result": "surprise"}), "unknown_result"), ((200, ValueError("not json")), "unreadable"),
    ((200, ["applied"]), "unreadable"), ((200, "applied"), "unreadable"), ((200, 7), "unreadable")])
async def test_a_route_no_answer_never_downgrades_a_verified_row(db, monkeypatch, answer, reason):
    """Review P3-A: re-checking a verified row and getting no readable answer keeps the verification and its
    reference cart, records only the reason, and backs off."""
    _on(monkeypatch)
    deal = await offer(db, "v")
    await seed(db, "s1")
    assert (await run(StubClient([APPLIED])))["applied"] == 1
    [before] = await verifications(db, deal)
    assert before["state"] == "checkout_verified"
    later = before["expires_at"].replace(tzinfo=NOW.tzinfo) - timedelta(hours=1)  # inside REVERIFY_WITHIN
    client = StubClient([answer])
    out = await run(client, now=later)
    assert len(client.calls) == 1, out
    assert out == {"due": 1, "route_errors": 1, "route_" + reason: 1, "carts": 1}, out
    [after] = await verifications(db, deal)
    assert after["state"] == "checkout_verified"
    for k in ("verified_at", "expires_at", "ref_unit_price", "ref_subtotal", "ref_discount_amount", "ref_currency"):
        assert after[k] == before[k], k
    assert after["failure_reason"] == "route:" + reason
    client = StubClient([APPLIED])
    await run(client, now=later + timedelta(minutes=30))
    assert client.calls == []


async def test_codes_in_markets_it_cannot_test_are_counted_by_name(db, monkeypatch):
    """Review N-6: GB, DE and no-market rows are never tested, and the summary says how many."""
    _on(monkeypatch)
    await offer(db, "gb", market="GB")
    await offer(db, "de", market="DE")
    await offer(db, "none", market=None)
    await offer(db, "us")
    await seed(db, "s1")
    out = await run(StubClient([APPLIED]))
    assert out["skipped_market_not_verifiable"] == 3 and out["applied"] == 1


async def test_the_out_of_market_count_is_absent_at_zero(db, monkeypatch):
    """Review P3-B: the key appears only when some row is out of market, so a normal summary is unchanged."""
    _on(monkeypatch)
    await offer(db, "us")
    await seed(db, "s1")
    out = await run(StubClient([APPLIED]))
    assert out == {"due": 1, "applied": 1, "carts": 1}, out


async def test_the_out_of_market_count_reads_only_crawl_rows_the_verifier_would_test(db, monkeypatch):
    """Review P3-B: a non-crawl row, a kind the verifier never tests and an ended deal are not counted; a lowercase
    market is not a market it tests, so it is."""
    _on(monkeypatch)
    await offer(db, "gb", market="GB")
    await offer(db, "gbnc", market="GB", crawl=False)
    await offer(db, "gbfs", market="GB", kind="free_shipping")
    await offer(db, "gbend", market="GB", ends_at=NOW - timedelta(days=30))
    await offer(db, "lower", market="us")
    out = await run(StubClient([]))
    assert out == {"due": 0, "skipped_market_not_verifiable": 2, "carts": 0}, out


async def test_no_cart_is_started_without_time_left_before_the_jobs_deadline(db, monkeypatch):
    _on(monkeypatch)
    await offer(db, "d")
    await seed(db, "s1")
    clock = [1000.0]
    client = StubClient([])
    out = await dv.run(now=NOW, client=client, sleep=_nosleep, monotonic=lambda: clock[0],
                       deadline=1000.0 + dv.CART_HEADROOM_SECONDS - 1)
    assert out["left_job_deadline"] == 1 and client.calls == []
    from db.deal_lane import deal_verify_host_daily_carts as caps

    assert await db.fetch_all(caps.select()) == []  # no slot taken either
    client = StubClient([APPLIED])
    out = await dv.run(now=NOW, client=client, sleep=_nosleep, monotonic=lambda: clock[0],
                       deadline=1000.0 + dv.CART_HEADROOM_SECONDS + 1)
    assert out["applied"] == 1


async def test_an_sg_code_is_verified_on_an_sgd_seed_stored_in_the_us_partition(db, monkeypatch):
    """Review P1-1, prod-shaped: the seed sits in partition 'US' with SGD prices."""
    _on(monkeypatch)
    deal = await offer(db, "sg", market="SG")
    await seed(db, "s1", currency="SGD")
    client = StubClient([(200, dict(APPLIED[1], currency="SGD"))])
    out = await run(client)
    assert out["applied"] == 1
    assert client.calls[0]["json"]["market"] == "SG" and client.calls[0]["json"]["expected_currency"] == "SGD"
    assert (await verifications(db, deal))[0]["ref_currency"] == "SGD"


async def test_each_hosts_seeds_are_read_once_per_run(db, monkeypatch):
    _on(monkeypatch)
    await offer(db, "m1", min_subtotal=100, currency="USD", observed=NOW - timedelta(hours=1))
    await offer(db, "m2", min_subtotal=200, currency="USD", observed=NOW - timedelta(hours=2))
    await seed(db, "s1")
    reads = []
    real = dv._host_seeds

    async def counting(host):
        reads.append(host)
        return await real(host)

    monkeypatch.setattr(dv, "_host_seeds", counting)
    out = await run(StubClient([]))
    assert out["skipped_min_subtotal_unmet"] == 2 and reads == ["glow.example"]


async def test_caps_one_cart_per_host_per_run_the_per_run_cap_and_the_daily_host_cap(db, monkeypatch):
    from db.deal_lane import deal_verify_host_daily_carts as caps

    _on(monkeypatch, DEAL_VERIFY_MAX_CARTS_PER_RUN=2, DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY=2)
    for i, host in enumerate(("glow.example", "glow.example", "shop.example", "brand.example")):
        await offer(db, f"c{i}", seller=host, observed=NOW - timedelta(hours=i + 1))
    for host in ("glow.example", "shop.example", "brand.example"):
        await seed(db, host, host=host)
    client = StubClient(NOT_PRICED)
    out = await run(client)
    assert out["carts"] == 2 and out["skipped_host_this_run"] == 1 and out["left_for_next_run"] == 1
    assert [c["json"]["seller_host"] for c in client.calls] == ["glow.example", "shop.example"]
    # The daily cap is counted in the database, across runs.
    assert await db.fetch_all(caps.select().where(caps.c.host == "brand.example")) == []  # no cart, no slot
    await db.execute(caps.insert().values(host="brand.example", utc_day=NOW.date(), carts=2, updated_at=NOW))
    client = StubClient(NOT_PRICED)
    out = await run(client, now=NOW + timedelta(minutes=5))
    assert out["skipped_host_cap"] == 1 and "brand.example" not in [c["json"]["seller_host"] for c in client.calls]


async def test_the_daily_slot_is_reserved_before_the_call_and_never_passes_the_cap(db, monkeypatch):
    _on(monkeypatch, DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY=2)
    assert await dv.reserve_cart("glow.example", NOW) is True
    assert await dv.reserve_cart("glow.example", NOW) is True
    assert await dv.reserve_cart("glow.example", NOW) is False
    assert await dv.reserve_cart("glow.example", NOW + timedelta(days=1)) is True  # a new UTC day
    assert await dv.reserve_cart("shop.example", NOW) is True


async def test_opted_out_sellers_feed_rows_and_other_writers_rows_get_no_cart(db, monkeypatch):
    from db.deal_lane import deal_lane_optout

    _on(monkeypatch)
    await offer(db, "o", seller="shop.example")
    await offer(db, "h", crawl=False)  # a merchant_published row this writer did not create
    await seed(db, "s1")
    await seed(db, "s2", host="shop.example")
    await db.execute(deal_lane_optout.insert().values(seller_id="www.shop.example", requested_at=NOW, recorded_by="t"))
    client = StubClient([])
    out = await run(client)
    assert out["skipped_optout"] == 1 and out["due"] == 1 and client.calls == []


async def test_an_unreadable_opt_out_or_refusal_table_means_no_cart(db, monkeypatch):
    from services import deal_code_capture_writer as writer

    _on(monkeypatch)
    await offer(db, "u")
    await seed(db, "s1")

    async def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(writer, "read_denyset", boom)
    client = StubClient([])
    assert await run(client) == {"optout_unreadable": 1} and client.calls == []
    monkeypatch.undo()
    _on(monkeypatch)
    await db.execute("DROP TABLE deal_code_refusals")
    try:
        client = StubClient([])
        assert await run(client) == {"refusals_unreadable": 1} and client.calls == []
    finally:
        from db import deal_lane
        from db.database import engine, metadata

        metadata.create_all(engine, tables=[deal_lane.deal_code_refusals])


async def test_no_reference_variant_means_no_cart_and_no_row(db, monkeypatch):
    _on(monkeypatch)
    deal = await offer(db, "m", min_subtotal=100, currency="USD")
    await seed(db, "s1")
    await offer(db, "x", seller="nothing.example")
    client = StubClient([])
    out = await run(client)
    assert out["skipped_min_subtotal_unmet"] == 1 and out["skipped_no_reference_variant"] == 1
    assert client.calls == [] and await verifications(db, deal) == []


async def test_nothing_raises_and_nothing_logged_names_a_code_or_a_host(db, monkeypatch, caplog):
    _on(monkeypatch)
    await offer(db, "l", seller="secret-shop.example", code="SECRET25")
    await seed(db, "s1", host="secret-shop.example")

    async def explode(*a, **k):
        raise RuntimeError("SECRET25 at secret-shop.example")

    monkeypatch.setattr(dv, "record", explode)
    caplog.set_level(logging.INFO)
    out = await run(StubClient([APPLIED]))
    assert out["errors"] == 1
    text = caplog.text + json.dumps(out)
    assert "SECRET25" not in text and "secret-shop" not in text


async def test_the_refresh_run_reports_the_verifier_only_when_it_ran(monkeypatch):
    import jobs.external_referral_refresh as job

    async def batch(**_):
        return {"status": "success"}

    async def retention():
        return {}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", batch)
    monkeypatch.setattr(job.deal_code_retention, "run", retention)

    async def off(**_):
        return {}

    monkeypatch.setattr(job.deal_code_verifier, "run", off)
    assert "deal_code_verifier" not in await job.run_daily_external_referral_refresh(limit=1)

    async def on(**_):
        return {"carts": 1, "applied": 1}

    monkeypatch.setattr(job.deal_code_verifier, "run", on)
    summary = await job.run_daily_external_referral_refresh(limit=1)
    assert summary["deal_code_verifier"] == {"carts": 1, "applied": 1}


async def test_the_refresh_run_gives_the_verifier_its_task_timeout_less_a_margin(monkeypatch):
    import jobs.external_referral_refresh as job

    async def batch(**_):
        return {"status": "success"}

    async def retention():
        return {}

    seen = {}

    async def verifier(**kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", batch)
    monkeypatch.setattr(job.deal_code_retention, "run", retention)
    monkeypatch.setattr(job.deal_code_verifier, "run", verifier)
    monkeypatch.delenv("EXTERNAL_REFERRAL_REFRESH_TASK_TIMEOUT_SECONDS", raising=False)
    # The deadline counts from the JOB's start, not the verifier's (review N-2): the batch takes 3300 s here.
    clock = [5000.0]

    async def slow_batch(**_):
        clock[0] += 3300.0
        return {"status": "success"}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", slow_batch)
    monkeypatch.setattr(job.time, "monotonic", lambda: clock[0])
    await job.run_daily_external_referral_refresh(limit=1)
    assert seen["deadline"] == 5000.0 + 3480.0
    monkeypatch.undo()
    for value, expected in (("7200", 7080.0), ("garbage", 3480.0), ("60", 3480.0), ("", 3480.0),
                            ("86400", 86280.0), ("86401", 3480.0), ("600", 480.0), ("599", 3480.0),
                            ("3600.5", 3480.0)):
        monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_TASK_TIMEOUT_SECONDS", value)
        assert job.verifier_deadline_seconds() == expected


async def test_retention_drops_refusal_hashes_after_30_days(db):
    from db.deal_lane import deal_code_refusals
    from services import deal_code_retention

    for name, days in (("old", 31), ("young", 29)):
        await db.execute(deal_code_refusals.insert().values(refusal_hash=name * 4, reason="rejected",
                                                            refused_at=NOW - timedelta(days=days)))
    out = await deal_code_retention.run(now=NOW)
    assert out["deleted_refusal_hashes"] == 1
    assert [r["refusal_hash"] for r in await db.fetch_all(deal_code_refusals.select())] == ["young" * 4]
    # Nothing to purge: no key, so the summary is the one it had before this rule (review P3-1).
    assert "deleted_refusal_hashes" not in await deal_code_retention.run(now=NOW)
