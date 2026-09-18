"""Tier B cart-link eligibility: the merchant list and the storage rules.

DIALECT-AGNOSTIC ON PURPOSE: nothing here is skipped on either engine. Run it on SQLite (the
default) and with a Postgres DATABASE_URL; tests/test_tierb_cart_link_eligibility_postgres.py
imports the database cases so the Postgres dialect gate (which runs only `*_postgres.py`) runs
them too, and adds what only Postgres can show (catalog parity with the migration, PREPARE).

Every rule test has an ACCEPT and a REFUSE half. The rules are the ones in
db/tierb_cart_link_eligibility.py's docstring: a definite verdict overwrites, an indefinite one
never does; `is_cart_link_eligible` is ELIGIBLE-and-fresh only; the key is normalised on write
AND read.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
import db.tierb_cart_link_eligibility as elig  # noqa: E402
from services.shopify_cart_link_preflight import PreflightResult, Verdict  # noqa: E402
from services.tierb_cart_link_merchants import (  # noqa: E402
    DEFAULT_MERCHANTS_PATH,
    Merchant,
    MerchantListError,
    load_merchants,
    normalize_domain,
    parse_merchants,
    select_merchants,
)

TABLE = "tierb_cart_link_eligibility"

_BRIEF_DEFINITE = {
    "ELIGIBLE", "LOGIN_REQUIRED", "NOT_ACCEPTING_ORDERS", "VARIANT_GONE", "VARIANT_UNAVAILABLE",
    "PASSWORD_PAGE", "BLOCKED_UNKNOWN", "CHECKOUT_PREFILL_MISSING", "CHECKOUT_MARKET_MISMATCH",
}
_BRIEF_INDEFINITE = {"TRANSPORT_ERROR", "VARIANT_UNVERIFIED", "UNCLASSIFIED", "INVALID_INPUT"}


# ── helpers ─────────────────────────────────────────────────────────────────────────────────


def res(verdict, host="judydoll.com", market="US", **over) -> PreflightResult:
    defaults = dict(
        host=host,
        verdict=Verdict(verdict),
        retryable=(Verdict(verdict) is Verdict.TRANSPORT_ERROR),
        market=market,
        variant_id="50041364447509",
        variant_source="caller",
        product_title="Single Eyeshadow",
        price="9.99",
        detail=None,
    )
    defaults.update(over)
    return PreflightResult(**defaults)


def _ago(hours: float) -> str:
    """A SERVER-SIDE timestamp `hours` ago, in the dialect's own expression — a test that bound
    its own datetime would exercise exactly the client-clock path the module avoids."""
    seconds = int(hours * 3600)
    if IS_POSTGRES:
        return f"CURRENT_TIMESTAMP - INTERVAL '{seconds} seconds'"
    return f"datetime('now', '-{seconds} seconds')"


async def _set(domain: str, market: str, column: str, hours_ago: float) -> None:
    await database.execute(
        f"UPDATE {TABLE} SET {column} = {_ago(hours_ago)} WHERE shop_domain = :d AND market = :m",
        {"d": domain, "m": market},
    )


async def _count() -> int:
    row = await database.fetch_one(f"SELECT COUNT(*) AS n FROM {TABLE}")
    return int(row["n"])


def _close(a: datetime, b: datetime, seconds: float = 120) -> bool:
    return abs((a - b).total_seconds()) <= seconds


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def eligibility_db():
    """Build the table the way production does — the self-heal's own DDL — from scratch."""
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await elig.ensure_schema()
    yield
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    # Each test runs on its own event loop; an asyncpg pool must not outlive the loop it was
    # opened on (the same convention as tests/test_reap_agentic_ledger_postgres.py).
    if not was_connected and database.is_connected:
        await database.disconnect()


# ── the merchant list ───────────────────────────────────────────────────────────────────────


def test_the_repo_merchant_list_loads_and_matches_the_measured_population():
    merchants = load_merchants()
    assert len(merchants) == 40
    assert len({(m.domain, m.market) for m in merchants}) == 40
    assert {m.market for m in merchants} == {"US", "JP", "SG"}
    assert sum(1 for m in merchants if m.variant_id) == 36
    by_domain = {m.domain: m for m in merchants}
    for known in ("forbeaut.us", "podl.us", "luafee.jp", "judydoll.com", "robinsons.com.sg"):
        assert known in by_domain
    assert by_domain["luafee.jp"].market == "JP"
    assert by_domain["robinsons.com.sg"].market == "SG"


# The seed variants replaced after the 2026-09-18 probe, and why. Every other row keeps the
# measured population's variant.
_REPLACED = {
    # the seed (Collagen Bubble Serum 1-Pack) is unavailable with country=US
    "podl.us": ("43311538634826", "43311735799882", "chestnut-balm-to-foam-cleanser"),
    # the seed 404s at /variants/<id>; a MAC lipstick live in SG replaces it
    "robinsons.com.sg": ("40975353675861", "42438682574933",
                         "powder-kiss-velvet-blur-slim-lipstick-898-sheer-outrage-2g-0-07oz-1"),
    # `--replace-unfit`: the seed was a sample / gift / trial kit, not something a buyer purchases
    "fentybeauty.com": ("67362637381677", "44306794217517", "gloss-bomb-universal-lip-luminizer-cherry-amor"),
    "haroutine.com": ("50679311892726", "51667887161590", "rest-restore-magnesium"),
    "medicube.us": ("43402263756848", "41946336886832", "pdrn-lip-sleeping-mask"),
    "goongbe.us": ("41008543563834", "40854246064186", "kids-moisture-lip-balm-0-1oz"),
}


def test_the_repo_merchant_list_carries_a_handle_for_every_seeded_variant():
    merchants = {m.domain: m for m in load_merchants()}
    seeded = [m for m in merchants.values() if m.variant_id]
    assert len(seeded) == 36 and all(m.product_handle for m in seeded)
    assert all(m.product_handle is None for m in merchants.values() if not m.variant_id)
    for domain, (_old, new, handle) in _REPLACED.items():
        assert (merchants[domain].variant_id, merchants[domain].product_handle) == (new, handle)
    assert merchants["metro.com.sg"].product_handle == "mac-m-a-cximal-matte-silky-lipstick"


def test_the_repo_merchant_list_is_the_2026_09_18_population_row_for_row():
    """The list is the measured population with `variant` renamed and a handle added: no row
    dropped or moved to another market, and no variant changed except those in _REPLACED."""
    population = os.environ.get("TIERB_POPULATION_JSON") or os.path.join(
        os.path.dirname(__file__), "..", "reports", "tierb_cart_permalink_2026_09_18", "population.json"
    )
    with open(DEFAULT_MERCHANTS_PATH, encoding="utf-8") as fh:
        ours = json.load(fh)
    assert all(set(r) <= {"domain", "market", "variant_id", "product_handle"} for r in ours)
    if os.path.exists(population):  # untracked report; present on the operator's checkout only
        with open(population, encoding="utf-8") as fh:
            theirs = json.load(fh)
        expected = [
            (r["domain"], r["market"],
             _REPLACED[r["domain"]][1] if r["domain"] in _REPLACED else r.get("variant"))
            for r in theirs
        ]
        assert expected == [(r["domain"], r["market"], r.get("variant_id")) for r in ours]
        for domain, (old, _new, _handle) in _REPLACED.items():
            assert any(r["domain"] == domain and r.get("variant") == old for r in theirs)


def _row(**over):
    row = {"domain": "judydoll.com", "market": "US", "variant_id": "50041364447509"}
    row.update(over)
    return row


def test_parse_accepts_a_canonical_row_and_one_without_a_variant():
    out = parse_merchants([_row(), {"domain": "podl.us", "market": "US"}])
    assert out == [
        Merchant("judydoll.com", "US", "50041364447509"),
        Merchant("podl.us", "US", None),
    ]


def test_parse_accepts_a_product_handle_hint_and_keeps_it_decoded():
    out = parse_merchants([_row(product_handle="mac-m-a-cximal-matte-silky-lipstick"),
                           {"domain": "podl.us", "market": "US", "variant_id": "1", "product_handle": "밤-클렌저"}])
    assert out[0].product_handle == "mac-m-a-cximal-matte-silky-lipstick"
    assert out[1].product_handle == "밤-클렌저"


@pytest.mark.parametrize("bad", ["", "a/b", "a?b", "a b", "a%20b", "x" * 256, 7, "a\nb"])
def test_parse_refuses_a_malformed_product_handle(bad):
    with pytest.raises(MerchantListError, match="product_handle"):
        parse_merchants([_row(product_handle=bad)])


def test_parse_refuses_a_handle_without_a_variant():
    with pytest.raises(MerchantListError, match="without variant_id"):
        parse_merchants([{"domain": "podl.us", "market": "US", "product_handle": "chestnut-balm-to-foam-cleanser"}])


def test_parse_accepts_the_same_domain_in_two_markets():
    out = parse_merchants([_row(market="US"), _row(market="SG")])
    assert [(m.domain, m.market) for m in out] == [("judydoll.com", "US"), ("judydoll.com", "SG")]


@pytest.mark.parametrize(
    "bad",
    [
        _row(domain="www.judydoll.com"),
        _row(domain="Judydoll.com"),
        _row(domain="https://judydoll.com"),
        _row(domain="judydoll.com/cart"),
        _row(domain="judydoll.com:443"),
        _row(domain="judydoll"),
        _row(domain="10.0.0.1"),
        _row(domain="judy_doll.com"),
        _row(domain=""),
        _row(domain=None),
        _row(market="us"),
        _row(market="USA"),
        _row(market="U1"),
        _row(market=None),
        _row(variant_id="abc"),
        _row(variant_id=50041364447509),
        _row(variant_id="gid://shopify/ProductVariant/1"),
        _row(varient_id="1"),
        "judydoll.com",
    ],
)
def test_parse_refuses_a_malformed_row(bad):
    with pytest.raises(MerchantListError):
        parse_merchants([_row(domain="podl.us"), bad])


def test_parse_refuses_a_duplicate_pair():
    with pytest.raises(MerchantListError, match="duplicate"):
        parse_merchants([_row(), _row(variant_id="1")])


@pytest.mark.parametrize("bad", [[], {}, None, {"merchants": [_row()]}])
def test_parse_refuses_an_empty_or_non_list_document(bad):
    with pytest.raises(MerchantListError):
        parse_merchants(bad)


def test_load_refuses_invalid_json(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("[{", encoding="utf-8")
    with pytest.raises(MerchantListError):
        load_merchants(path)


def test_select_merchants_normalises_the_filter_and_refuses_an_unlisted_domain():
    merchants = parse_merchants([_row(), {"domain": "podl.us", "market": "US"}])
    assert [m.domain for m in select_merchants(merchants, ["www.JudyDoll.com"])] == ["judydoll.com"]
    assert select_merchants(merchants, None) == merchants
    with pytest.raises(MerchantListError, match="not on the merchant list"):
        select_merchants(merchants, ["someoneelse.com"])


@pytest.mark.parametrize(
    "raw, want",
    [
        ("judydoll.com", "judydoll.com"),
        ("www.Judydoll.com", "judydoll.com"),
        ("  WWW.JUDYDOLL.COM.  ", "judydoll.com"),
        ("robinsons.com.sg", "robinsons.com.sg"),
        ("www2.judydoll.com", "www2.judydoll.com"),
    ],
)
def test_normalize_domain_accepts(raw, want):
    assert normalize_domain(raw) == want


@pytest.mark.parametrize(
    "raw", ["", "www.", "localhost", "https://judydoll.com", "judydoll.com/x", "judydoll.com:8443",
            "a@judydoll.com", "1.2.3.4", "[::1]", None, 42]
)
def test_normalize_domain_refuses(raw):
    with pytest.raises(ValueError):
        normalize_domain(raw)


# ── the verdict partition ───────────────────────────────────────────────────────────────────


def test_every_preflight_verdict_is_classified_definite_or_indefinite():
    """FAILS THE MOMENT #2209's Verdict enum gains a member this module has not classified, and
    names it. Classifying one is a decision (does it overwrite a verdict? is it eligible?) and
    touches three places: the set in db/tierb_cart_link_eligibility.py and, for a definite one,
    the verdict CHECK lists in migration 227 and in the self-heal."""
    missing = elig.unclassified_verdicts()
    assert not missing, (
        f"Verdict member(s) {missing} are classified NEITHER definite NOR indefinite in "
        "db/tierb_cart_link_eligibility.py; record_result refuses them and the job exits 4. "
        "Decide, add them to DEFINITE_VERDICTS or INDEFINITE_VERDICTS, and (if definite) to the "
        "verdict/previous_verdict CHECK lists in db/migrations/227_* and the self-heal."
    )


def test_the_unclassified_check_names_a_new_member():
    """The check above is only as good as its detector: a synthetic enum with one extra member
    must come back named, and only that member."""
    import enum

    Grown = enum.Enum("Grown", {v.name: v.value for v in Verdict} | {"SHOP_ON_FIRE": "SHOP_ON_FIRE"})
    assert elig.unclassified_verdicts(Grown) == ["SHOP_ON_FIRE"]
    assert elig.unclassified_verdicts(Verdict) == []


def test_the_definite_and_indefinite_sets_are_the_decided_ones_and_disjoint():
    definite = {v.value for v in elig.DEFINITE_VERDICTS}
    indefinite = {v.value for v in elig.INDEFINITE_VERDICTS}
    assert definite == _BRIEF_DEFINITE
    assert indefinite == _BRIEF_INDEFINITE
    assert not definite & indefinite
    assert definite | indefinite == {v.value for v in Verdict}


async def test_a_verdict_in_neither_set_is_refused_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(elig, "DEFINITE_VERDICTS", elig.DEFINITE_VERDICTS - {Verdict.ELIGIBLE})
    with pytest.raises(ValueError, match="neither definite nor indefinite"):
        await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert await _count() == 0


# ── schema ──────────────────────────────────────────────────────────────────────────────────


async def test_the_schema_guard_self_heal_creates_the_table():
    from db.schema_guard import ensure_required_schema_light

    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await ensure_required_schema_light()
    row = await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert row["verdict"] == "ELIGIBLE"


async def test_ensure_schema_is_idempotent():
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await elig.ensure_schema()
    assert (await elig.get_eligibility("judydoll.com", "US"))["verdict"] == "ELIGIBLE"


async def test_the_database_refuses_an_indefinite_verdict_in_the_verdict_column():
    with pytest.raises(Exception):
        await database.execute(
            f"INSERT INTO {TABLE} (shop_domain, market, verdict, checked_at) "
            "VALUES ('judydoll.com', 'US', 'TRANSPORT_ERROR', CURRENT_TIMESTAMP)"
        )
    assert await _count() == 0


async def test_the_database_refuses_a_verdict_without_its_clock():
    with pytest.raises(Exception):
        await database.execute(
            f"INSERT INTO {TABLE} (shop_domain, market, verdict) VALUES ('judydoll.com', 'US', 'ELIGIBLE')"
        )
    with pytest.raises(Exception):
        await database.execute(
            f"INSERT INTO {TABLE} (shop_domain, market, checked_at) "
            "VALUES ('judydoll.com', 'US', CURRENT_TIMESTAMP)"
        )
    assert await _count() == 0


async def test_the_database_refuses_a_lowercase_market_and_a_second_row_for_the_same_key():
    with pytest.raises(Exception):
        await database.execute(f"INSERT INTO {TABLE} (shop_domain, market) VALUES ('judydoll.com', 'us')")
    await database.execute(f"INSERT INTO {TABLE} (shop_domain, market) VALUES ('judydoll.com', 'US')")
    with pytest.raises(Exception):
        await database.execute(f"INSERT INTO {TABLE} (shop_domain, market) VALUES ('judydoll.com', 'US')")
    assert await _count() == 1


# ── DEFINITE: overwrite ─────────────────────────────────────────────────────────────────────


async def test_a_first_definite_verdict_inserts_the_row_with_its_clock_and_evidence():
    row = await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert row["shop_domain"] == "judydoll.com" and row["market"] == "US"
    assert row["verdict"] == "ELIGIBLE"
    assert row["retryable"] is False
    assert row["variant_id"] == "50041364447509"
    assert row["variant_source"] == "caller"
    assert row["product_title"] == "Single Eyeshadow"
    assert row["price_text"] == "9.99"
    assert row["consecutive_same"] == 1
    assert row["previous_verdict"] is None
    assert row["last_error_code"] is None
    for column in ("checked_at", "last_attempt_at", "verdict_changed_at", "created_at"):
        assert isinstance(row[column], datetime) and row[column].tzinfo is not None, column
        assert _close(row[column], _now()), column
    assert await elig.get_eligibility("judydoll.com", "US") == row


async def test_a_repeated_definite_verdict_counts_up_and_keeps_its_change_clock():
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await _set("judydoll.com", "US", "verdict_changed_at", 30)
    await _set("judydoll.com", "US", "checked_at", 24)
    before = await elig.get_eligibility("judydoll.com", "US")

    row = await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert row["verdict"] == "ELIGIBLE"
    assert row["consecutive_same"] == 2
    assert row["previous_verdict"] is None
    assert row["verdict_changed_at"] == before["verdict_changed_at"]  # REFUSE: no change, no stamp
    assert _close(row["checked_at"], _now())  # ACCEPT: the verdict clock moved
    assert row["checked_at"] > before["checked_at"]

    row = await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert row["consecutive_same"] == 3


async def test_a_changed_definite_verdict_resets_the_count_and_records_what_it_replaced():
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await _set("judydoll.com", "US", "verdict_changed_at", 30)
    before = await elig.get_eligibility("judydoll.com", "US")
    assert before["consecutive_same"] == 2

    row = await elig.record_result("judydoll.com", "US", res("LOGIN_REQUIRED", variant_id=None, price=None))
    assert row["verdict"] == "LOGIN_REQUIRED"
    assert row["previous_verdict"] == "ELIGIBLE"
    assert row["consecutive_same"] == 1
    assert row["verdict_changed_at"] > before["verdict_changed_at"]
    assert _close(row["verdict_changed_at"], _now())
    # The evidence is the NEW observation's, even where that is "none".
    assert row["variant_id"] is None and row["price_text"] is None

    row = await elig.record_result("judydoll.com", "US", res("LOGIN_REQUIRED"))
    assert row["consecutive_same"] == 2
    assert row["previous_verdict"] == "ELIGIBLE"  # one step deep: kept while the verdict holds


@pytest.mark.parametrize("verdict", sorted(_BRIEF_DEFINITE))
async def test_every_definite_verdict_overwrites_a_prior_one(verdict):
    other = "VARIANT_GONE" if verdict != "VARIANT_GONE" else "ELIGIBLE"
    await elig.record_result("judydoll.com", "US", res(other))
    row = await elig.record_result("judydoll.com", "US", res(verdict, detail="d"))
    assert row["verdict"] == verdict
    assert row["previous_verdict"] == other
    assert row["detail"] == "d"


async def test_a_definite_verdict_clears_the_last_error_and_fills_a_null_row():
    await elig.record_result("judydoll.com", "US", res("TRANSPORT_ERROR", detail="resolve:ConnectError"))
    row = await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert row["verdict"] == "ELIGIBLE"
    assert row["last_error_code"] is None
    assert row["previous_verdict"] is None  # NULL -> ELIGIBLE: nothing definite was replaced
    assert row["consecutive_same"] == 1
    assert row["verdict_changed_at"] is not None


# ── INDEFINITE: never overwrite ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("indefinite", sorted(_BRIEF_INDEFINITE))
async def test_an_indefinite_result_never_overwrites_a_prior_verdict(indefinite):
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await _set("judydoll.com", "US", "checked_at", 20)
    await _set("judydoll.com", "US", "last_attempt_at", 20)
    before = await elig.get_eligibility("judydoll.com", "US")

    row = await elig.record_result(
        "judydoll.com", "US",
        res(indefinite, detail="resolve:ConnectError", variant_id="999", product_title="Other",
            price="1.00", variant_source="catalog"),
    )
    # REFUSE: nothing about the verdict moved.
    for column in ("verdict", "checked_at", "consecutive_same", "previous_verdict", "verdict_changed_at",
                   "variant_id", "variant_source", "product_title", "price_text", "detail", "retryable"):
        assert row[column] == before[column], column
    # ACCEPT: the attempt is recorded.
    assert row["last_attempt_at"] > before["last_attempt_at"]
    assert _close(row["last_attempt_at"], _now())
    assert row["last_error_code"] == f"{indefinite}:resolve:ConnectError"
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is True


async def test_eligible_then_transport_error_keeps_eligible_with_the_old_checked_at():
    """The brief's worked example, verbatim."""
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await _set("judydoll.com", "US", "checked_at", 5)
    old = (await elig.get_eligibility("judydoll.com", "US"))["checked_at"]
    row = await elig.record_result("judydoll.com", "US", res("TRANSPORT_ERROR", detail="permalink:ConnectError"))
    assert row["verdict"] == "ELIGIBLE"
    assert row["checked_at"] == old
    assert row["last_error_code"] == "TRANSPORT_ERROR:permalink:ConnectError"


async def test_an_indefinite_result_with_no_prior_row_inserts_a_null_verdict():
    row = await elig.record_result("judydoll.com", "US", res("VARIANT_UNVERIFIED", detail="catalog_scan_cap"))
    assert row["verdict"] is None
    assert row["checked_at"] is None
    assert row["verdict_changed_at"] is None
    assert row["previous_verdict"] is None
    assert row["consecutive_same"] == 0
    assert row["variant_id"] is None  # evidence of a non-answer is not recorded
    assert row["last_error_code"] == "VARIANT_UNVERIFIED:catalog_scan_cap"
    assert _close(row["last_attempt_at"], _now())
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is False


async def test_an_indefinite_error_code_without_detail_is_the_verdict_and_is_bounded():
    row = await elig.record_result("judydoll.com", "US", res("UNCLASSIFIED", detail=None))
    assert row["last_error_code"] == "UNCLASSIFIED"
    row = await elig.record_result("judydoll.com", "US", res("UNCLASSIFIED", detail="x" * 500))
    assert len(row["last_error_code"]) == 128


async def test_an_overlong_evidence_value_is_truncated_on_both_dialects():
    row = await elig.record_result(
        "judydoll.com", "US", res("BLOCKED_UNKNOWN", detail="d" * 400, price="9" * 50)
    )
    assert row["detail"] == "d" * 255
    assert row["price_text"] == "9" * 32


# ── is_cart_link_eligible ───────────────────────────────────────────────────────────────────


async def test_eligible_and_fresh_is_eligible():
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is True
    await _set("judydoll.com", "US", "checked_at", 47)
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is True


async def test_eligible_but_stale_is_not_eligible():
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await _set("judydoll.com", "US", "checked_at", 49)
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is False
    assert await elig.is_cart_link_eligible("judydoll.com", "US", max_age_hours=50) is True
    await _set("judydoll.com", "US", "checked_at", 2)
    assert await elig.is_cart_link_eligible("judydoll.com", "US", max_age_hours=1) is False
    assert await elig.is_cart_link_eligible("judydoll.com", "US", max_age_hours=3) is True


async def test_a_recent_attempt_does_not_refresh_a_stale_eligible():
    """Freshness is measured on checked_at, never last_attempt_at: a stream of transport errors
    must let ELIGIBLE age out."""
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    await _set("judydoll.com", "US", "checked_at", 72)
    await elig.record_result("judydoll.com", "US", res("TRANSPORT_ERROR"))
    row = await elig.get_eligibility("judydoll.com", "US")
    assert row["verdict"] == "ELIGIBLE" and _close(row["last_attempt_at"], _now())
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is False


@pytest.mark.parametrize("verdict", sorted(_BRIEF_DEFINITE - {"ELIGIBLE"}))
async def test_every_other_fresh_definite_verdict_is_not_eligible(verdict):
    await elig.record_result("judydoll.com", "US", res(verdict))
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is False


async def test_no_row_and_a_null_verdict_are_not_eligible():
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is False
    await elig.record_result("judydoll.com", "US", res("TRANSPORT_ERROR"))
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is False


async def test_eligibility_is_per_market():
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is True
    assert await elig.is_cart_link_eligible("judydoll.com", "SG") is False


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "48", None])
async def test_a_nonsense_max_age_is_refused(bad):
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    with pytest.raises(ValueError):
        await elig.is_cart_link_eligible("judydoll.com", "US", max_age_hours=bad)


# ── the key ─────────────────────────────────────────────────────────────────────────────────


async def test_www_and_case_variants_hit_one_row_on_write_and_on_read():
    await elig.record_result("www.Judydoll.com", "us", res("ELIGIBLE", host="www.Judydoll.com"))
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE"))
    assert await _count() == 1
    row = await elig.get_eligibility("judydoll.com", "US")
    assert row["shop_domain"] == "judydoll.com" and row["market"] == "US"
    assert row["consecutive_same"] == 2
    assert await elig.get_eligibility("WWW.JUDYDOLL.COM", "us") == row
    assert await elig.is_cart_link_eligible("www.judydoll.com", "us") is True


async def test_a_result_for_another_store_is_refused_and_writes_nothing():
    with pytest.raises(ValueError, match="not 'judydoll.com'"):
        await elig.record_result("judydoll.com", "US", res("ELIGIBLE", host="podl.us"))
    assert await _count() == 0


async def test_a_result_read_for_another_market_is_refused_and_writes_nothing():
    with pytest.raises(ValueError, match="market"):
        await elig.record_result("judydoll.com", "SG", res("ELIGIBLE", market="US"))
    assert await _count() == 0
    # ACCEPT: a refused-input result carries no market and is still recordable (indefinite).
    row = await elig.record_result("judydoll.com", "SG", res("INVALID_INPUT", market=None))
    assert row["verdict"] is None


@pytest.mark.parametrize("domain, market", [("", "US"), ("https://judydoll.com", "US"), ("judydoll.com", "USA"), ("judydoll.com", "")])
async def test_a_malformed_key_is_refused_on_write_and_on_read(domain, market):
    with pytest.raises(ValueError):
        await elig.record_result(domain, market, res("ELIGIBLE", host="judydoll.com"))
    with pytest.raises(ValueError):
        await elig.get_eligibility(domain, market)
    with pytest.raises(ValueError):
        await elig.is_cart_link_eligible(domain, market)
    assert await _count() == 0


# ── CHECKOUT_MARKET_MISMATCH and checkout_country (#2209 @ 4e12d5a2) ────────────────────────


async def test_a_market_mismatch_is_definite_overwrites_eligible_and_is_not_eligible():
    await elig.record_result("judydoll.com", "US", res("ELIGIBLE", checkout_country="US"))
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is True
    row = await elig.record_result(
        "judydoll.com", "US", res("CHECKOUT_MARKET_MISMATCH", checkout_country="JP", detail="checkout_country_JP")
    )
    assert row["verdict"] == "CHECKOUT_MARKET_MISMATCH"  # ACCEPT: recorded as a verdict
    assert row["previous_verdict"] == "ELIGIBLE"
    assert row["consecutive_same"] == 1
    assert row["checkout_country"] == "JP"
    assert _close(row["checked_at"], _now())
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is False  # REFUSE: not eligible


async def test_a_market_mismatch_is_accepted_as_a_previous_verdict_too():
    await elig.record_result("judydoll.com", "US", res("CHECKOUT_MARKET_MISMATCH", checkout_country=None))
    row = await elig.record_result("judydoll.com", "US", res("ELIGIBLE", checkout_country="US"))
    assert row["previous_verdict"] == "CHECKOUT_MARKET_MISMATCH"
    assert await elig.is_cart_link_eligible("judydoll.com", "US") is True


async def test_checkout_country_is_evidence_of_the_definite_verdict_only():
    row = await elig.record_result("judydoll.com", "US", res("ELIGIBLE", checkout_country="US"))
    assert row["checkout_country"] == "US"
    # an indefinite result never touches it
    row = await elig.record_result("judydoll.com", "US", res("UNCLASSIFIED", checkout_country="JP"))
    assert row["checkout_country"] == "US"
    # a definite one replaces it, with NULL when the page stated none
    row = await elig.record_result("judydoll.com", "US", res("LOGIN_REQUIRED", checkout_country=None))
    assert row["checkout_country"] is None


async def test_an_indefinite_first_row_has_no_checkout_country():
    row = await elig.record_result("judydoll.com", "US", res("TRANSPORT_ERROR", checkout_country="US"))
    assert row["checkout_country"] is None


@pytest.mark.parametrize("bad", ["us", "USA", "U", "", "1A", 42])
async def test_a_malformed_checkout_country_is_stored_as_null_not_truncated(bad):
    row = await elig.record_result("judydoll.com", "US", res("CHECKOUT_MARKET_MISMATCH", checkout_country=bad))
    assert row["verdict"] == "CHECKOUT_MARKET_MISMATCH"
    assert row["checkout_country"] is None


async def test_the_database_refuses_a_malformed_checkout_country():
    with pytest.raises(Exception):
        await database.execute(
            f"INSERT INTO {TABLE} (shop_domain, market, checkout_country) VALUES ('judydoll.com', 'US', 'us')"
        )
    assert await _count() == 0
