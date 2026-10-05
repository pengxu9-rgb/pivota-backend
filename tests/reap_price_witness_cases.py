"""The price witness (mig 258): corroborated price change, buy-intent preflight, live price view.

NOT A TEST MODULE ITSELF. Collected under BOTH dialects by tests/test_reap_price_witness.py
(SQLite) and tests/test_reap_price_witness_postgres.py (Postgres), exactly like
tests/reap_cart_link_cases.py, whose fixtures (schema, dials, network ban, fake client,
attribution) this module reuses unchanged.

REAL HERE: the ledger, the schema (self-heal / migrations), the verifier, the witness writes, the
storefront-proof READS -- and the proof ROWS themselves, which are written by the real producers:
  * enrichment proofs: jobs/enrichment_cart_variant_proof (`evidence_from_product` ->
    `decide_proof` -> `upsert_proof`) over the REAL tarte `.js` body read 2026-09-29
    (tests/fixtures/enrichment_cart_proof_storefronts_2026_09_29.json), against catalog rows built
    by the enrichment lane's own producers (tests/test_enrichment_cart_variant_proof_job.TARTE);
  * mirror proofs: scripts/backfill_shopify_variant_ids.build_cart_proof over the REAL judydoll
    `.js` and seed fixtures.
FAKE: every Reap call (the cart-link FakeReap), and the attribution hook.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import db.reap_agentic_ledger as ledger
import db.reap_price_witness as witness
import services.reap_agentic_client as rc
import services.reap_agentic_purchase as svc
import services.reap_price_corroboration as corroboration
from db.database import IS_POSTGRES, database

from reap_cart_link_cases import (  # noqa: F401 -- the autouse fixtures are collected through this module
    CLICK,
    attribution,
    cartlink_db,
    cartlink_env,
    cartlink_no_network,
    reap,
    active_enrollment,
    cart_quote,
    get,
    item,
    ok,
    start,
    step,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# ── the enrichment storefront (real tarte `.js`, real catalog producers) ─────────────────────

from jobs.enrichment_cart_variant_proof import (  # noqa: E402
    SOURCE_PRODUCTS_JS,
    decide_proof,
    evidence_from_product,
    upsert_proof,
)
from test_enrichment_cart_variant_proof_job import TARTE, target  # noqa: E402

TARTE_HOST = "tartecosmetics.com"
BERRY = "63530896818545"  # $30.00, the one AVAILABLE shade in the fixture
TARTE_PK = TARTE["product"]["product_key"]
_STOREFRONTS = json.loads(
    (FIXTURES / "enrichment_cart_proof_storefronts_2026_09_29.json").read_text(encoding="utf-8")
)


def tarte_js(price=None):
    body = json.loads(json.dumps(_STOREFRONTS["tarte_js_amazonian_clay_baked_blush"]["body"]),
                      parse_float=Decimal)
    if price is not None:
        for variant in body["variants"]:
            if str(variant["id"]) == BERRY:
                variant["price"] = price
    return body


async def write_enrichment_proof(*, age=timedelta(hours=1), currency="USD", js_price=None):
    """One proof row for the berry sku, written by the proof JOB's own functions."""
    import db.enrichment_cart_variant_proofs as proofs

    assert await proofs.ensure_table()
    checked = datetime.now(timezone.utc) - age
    body = tarte_js(js_price)
    evidence = evidence_from_product(body, requested_handle=body["handle"], source=SOURCE_PRODUCTS_JS,
                                     checked_at=checked, currency=currency, currency_problem=None)
    row = decide_proof(target(TARTE, "::v:" + BERRY), evidence, market_currency=currency)
    assert row.outcome == "ok", row.outcome
    assert await upsert_proof(database, row, written_at=checked)
    return row


@pytest.fixture(autouse=True)
async def witness_proofs_cleanup():
    import db.enrichment_cart_variant_proofs as proofs

    proofs._reset_for_tests()
    yield
    try:
        await database.execute(
            "DELETE FROM enrichment_cart_variant_proofs WHERE product_key = :pk", {"pk": TARTE_PK}
        )
    except Exception:  # noqa: BLE001 - a table this test never created
        pass
    proofs._reset_for_tests()


@pytest.fixture(autouse=True)
def witness_dials(monkeypatch):
    for name in (svc.REAP_AGENTIC_PRICE_CORROBORATION_ENV, svc.REAP_AGENTIC_PREFLIGHT_MODE_ENV,
                 svc.REAP_AGENTIC_CORROBORATION_MAX_AGE_HOURS_ENV, svc.REAP_AGENTIC_PILOT_SCOPE_ENV):
        monkeypatch.delenv(name, raising=False)


def corroboration_on(monkeypatch):
    monkeypatch.setenv(svc.REAP_AGENTIC_PRICE_CORROBORATION_ENV, "on")


def tarte_url(quantity=1, country="US", variant=BERRY, click=CLICK):
    return f"https://{TARTE_HOST}/cart/{variant}:{quantity}?attributes[pivota_click_id]={click}&country={country}"


async def open_tarte(our_price, *, quantity=1, currency="USD", market="US", enrolled=True):
    if enrolled:
        await active_enrollment()
    return await start(
        cart_link=item(cart_url=tarte_url(quantity, market), shop_domain=TARTE_HOST,
                       our_price_minor=our_price, currency=currency, market_country=market,
                       product_key=TARTE_PK, product_name="Amazonian clay baked blush"),
        quantity=quantity,
    )


def tarte_quote(subtotal=30.0, shipping=5.0, currency="USD", quote_id="q_cart_1", **over):
    """`cart_quote`'s LIVE shape with other amounts."""
    body = copy.deepcopy(cart_quote())
    body["id"] = quote_id
    breakdown = body["amountBreakdown"]
    breakdown["itemsSubtotal"] = {"amount": subtotal, "currency": currency}
    breakdown["shipping"] = {"amount": shipping, "currency": currency}
    breakdown["tax"] = {"amount": {"amount": 0, "currency": currency}}
    breakdown["finalAmount"] = {"amount": round(subtotal + shipping, 2), "currency": currency}
    for option in body["shippingOptions"]:
        option["price"]["currency"] = currency
    body["shippingOptions"][0]["price"]["amount"] = shipping
    body.update(over)
    return body


async def quote_with(reap, quote, *, our_price, **kwargs):
    reap.request_cart_link_quote = ok(quote)
    purchase_id = await open_tarte(our_price, **kwargs)
    moved = await step(purchase_id)
    assert moved.state == "quoting", moved
    return purchase_id, await step(purchase_id)


def picture(row):
    return {key: row.get(key) for key in (
        "live_unit_price_minor", "live_items_subtotal_minor", "live_quoted_total_minor",
        "live_price_stage", "price_rebound_from_minor", "price_rebound_to_minor",
        "price_corroboration_source")}


NO_PICTURE = {key: None for key in picture({})}


def public_body(row):
    import routes.agent_commerce_reap as route

    return route._public_body(ledger.public_purchase_view(row))


# ══ 0. dials ═════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("value,armed", [
    (None, False), ("", False), ("off", False), ("0", False), ("ture", False),
    ("on", True), ("1", True), (" TRUE ", True),
])
def test_the_corroboration_dial_is_strict_and_off_by_default(monkeypatch, value, armed):
    if value is not None:
        monkeypatch.setenv(svc.REAP_AGENTIC_PRICE_CORROBORATION_ENV, value)
    assert svc.is_price_corroboration_enabled() is armed


@pytest.mark.parametrize("value,mode", [
    (None, "off"), ("", "off"), ("on", "off"), ("enforced", "off"), ("SHADOW", "shadow"),
    (" enforce ", "enforce"), ("off", "off"),
])
def test_the_preflight_mode_is_off_unless_named_exactly(monkeypatch, value, mode):
    if value is not None:
        monkeypatch.setenv(svc.REAP_AGENTIC_PREFLIGHT_MODE_ENV, value)
    assert svc.preflight_mode() == mode


@pytest.mark.parametrize("value,hours", [
    (None, 72), ("", 72), ("24", 24), ("168", 168), ("169", 72), ("0", 72), ("-3", 72), ("x", 72),
])
def test_the_corroboration_window_has_sane_bounds(monkeypatch, value, hours):
    if value is not None:
        monkeypatch.setenv(svc.REAP_AGENTIC_CORROBORATION_MAX_AGE_HOURS_ENV, value)
    assert svc.corroboration_max_age() == timedelta(hours=hours)


# ══ 1. dials off: nothing changes ════════════════════════════════════════════════════════════


async def test_dials_off_the_price_changed_refusal_is_byte_identical(reap, monkeypatch):
    """The defect this PR is about, with every dial off: refused exactly as before, no witness
    column touched, no proof read, no new key in the GET body, one quote with today's kwargs."""
    async def forbidden(*a, **k):
        raise AssertionError("a witness read/write ran with the dials off")

    for name in ("enrichment_proofs_for_variant", "mirror_seed_for_product", "record_live_price",
                 "record_preflight", "begin_preflight"):
        monkeypatch.setattr(witness, name, forbidden)
    await write_enrichment_proof()  # a corroborating proof exists, and must not be consulted
    purchase_id, result = await quote_with(reap, tarte_quote(30.0), our_price=2800)
    assert (result.state, result.refusal_reason, result.last_error_code) == (
        "refused", "price_changed", "quote_items_subtotal_mismatch")
    row = await get(purchase_id)
    assert {k: row.get(k) for k in witness.COLUMNS} == {k: None for k in witness.COLUMNS}
    body = public_body(row)
    assert not {"preflight", "live_price", "price_rebound"} & set(body)
    assert not set(witness.COLUMNS) & set(body)
    (sent,) = reap.named("request_cart_link_quote")
    assert "idempotency_extra" not in sent
    assert reap.sequence().count("create_enrollment") == 0


def test_dials_off_the_verifier_returns_at_the_subtotal_as_before():
    """`defer_subtotal_mismatch` defaults off: the same early return, no amounts, and the
    early return still wins over an unreconciled total (today's precedence)."""
    row = {"currency": "USD", "our_price_minor": 2800, "quantity": 1}
    unreconciled = tarte_quote(30.0)
    unreconciled["amountBreakdown"]["finalAmount"]["amount"] = 99.0
    for quote in (tarte_quote(30.0), unreconciled):
        check = svc.verify_cart_link_quote(quote, row)
        assert check == svc.QuoteCheck(False, "price_changed", "quote_items_subtotal_mismatch")
        assert check.subtotal_mismatch_only is False and check.total_minor is None


async def test_dials_off_the_enrollment_step_quotes_nothing(reap):
    """No preflight: 'resolving' -> 'needs_enrollment' with exactly today's call sequence."""
    purchase_id = await open_tarte(3000, enrolled=False)
    result = await step(purchase_id)
    assert result.state == "needs_enrollment"
    assert reap.sequence() == ["create_enrollment"]
    assert (await get(purchase_id))["preflight_outcome"] is None


# ══ 2. Part 1: the corroborated price change at the approval quote ═══════════════════════════


async def test_a_lower_corroborated_price_continues_at_the_quote(reap, monkeypatch):
    corroboration_on(monkeypatch)
    await write_enrichment_proof()  # live $30.00
    purchase_id, result = await quote_with(reap, tarte_quote(30.0), our_price=3200)
    assert result.state == "awaiting_approval", result
    row = await get(purchase_id)
    assert picture(row) == {
        "live_unit_price_minor": 3000, "live_items_subtotal_minor": 3000,
        "live_quoted_total_minor": 3500, "live_price_stage": "approval",
        "price_rebound_from_minor": 3200, "price_rebound_to_minor": 3000,
        "price_corroboration_source": "enrichment_proof"}
    # THE BUYER'S SELECTED PRICE IS NOT REWRITTEN; the charge is Reap's own (lower) quote total.
    assert row["our_price_minor"] == 3200 and row["quoted_total_minor"] == 3500
    assert row["price_corroborated_at"] is not None
    (checkout,) = reap.named("create_checkout")
    assert checkout["quote_id"] == "q_cart_1"
    body = public_body(row)
    assert body["price_rebound"] == {
        "currency": "USD", "from_unit_price_minor": 3200, "to_unit_price_minor": 3000,
        "source": "enrichment_proof", "corroborated_at": row["price_corroborated_at"]}
    assert "live_price" not in body  # not refused


async def test_a_higher_corroborated_price_refuses_with_its_own_code(reap, monkeypatch):
    corroboration_on(monkeypatch)
    await write_enrichment_proof()
    purchase_id, result = await quote_with(reap, tarte_quote(30.0), our_price=2800)
    assert (result.state, result.refusal_reason, result.last_error_code) == (
        "refused", "price_changed", "quote_price_increased_corroborated")
    row = await get(purchase_id)
    assert picture(row) == {**NO_PICTURE, "live_unit_price_minor": 3000,
                            "live_items_subtotal_minor": 3000, "live_quoted_total_minor": 3500,
                            "live_price_stage": "approval"}
    assert reap.named("create_checkout") == []
    assert public_body(row)["live_price"] == {
        "currency": "USD", "unit_price_minor": 3000, "items_subtotal_minor": 3000,
        "quoted_total_minor": 3500, "stage": "approval"}


@pytest.mark.parametrize("case", ["no_proof", "stale_proof", "disagreeing_proof", "foreign_currency"])
@pytest.mark.parametrize("our_price", [3200, 2800], ids=["lower", "higher"])
async def test_without_corroboration_it_refuses_as_today_and_records_the_live_price(
        reap, monkeypatch, case, our_price):
    """Each guard in turn, in BOTH directions -- deleting any one of them (the freshness window,
    the price equality, the currency equality) turns one of these into a continue or a
    `quote_price_increased_corroborated`."""
    corroboration_on(monkeypatch)
    currency, market = "USD", "US"
    if case == "stale_proof":
        await write_enrichment_proof(age=timedelta(hours=73))
    elif case == "disagreeing_proof":
        await write_enrichment_proof(js_price=2900)  # our own read says $29.00, Reap $30.00
    elif case == "foreign_currency":
        # The purchase is in GBP (a GB market cart); our only read of this variant is the US
        # storefront's USD $30.00 -- the same number, another currency. Not corroboration.
        await write_enrichment_proof(currency="USD")
        currency, market = "GBP", "GB"
    quote = tarte_quote(30.0, currency=currency)
    purchase_id, result = await quote_with(reap, quote, our_price=our_price,
                                           currency=currency, market=market)
    assert (result.state, result.refusal_reason, result.last_error_code) == (
        "refused", "price_changed", "quote_items_subtotal_mismatch"), result
    row = await get(purchase_id)
    assert picture(row) == {**NO_PICTURE, "live_unit_price_minor": 3000,
                            "live_items_subtotal_minor": 3000, "live_quoted_total_minor": 3500,
                            "live_price_stage": "approval"}
    assert public_body(row)["live_price"]["unit_price_minor"] == 3000
    assert reap.named("create_checkout") == []


async def test_the_freshness_window_is_the_dial(reap, monkeypatch):
    """A 73-hour-old read corroborates once the operator widens the window -- the window is read,
    not a constant."""
    corroboration_on(monkeypatch)
    monkeypatch.setenv(svc.REAP_AGENTIC_CORROBORATION_MAX_AGE_HOURS_ENV, "96")
    await write_enrichment_proof(age=timedelta(hours=73))
    _purchase_id, result = await quote_with(reap, tarte_quote(30.0), our_price=3200)
    assert result.state == "awaiting_approval"


async def test_a_future_dated_proof_never_corroborates(reap, monkeypatch):
    corroboration_on(monkeypatch)
    await write_enrichment_proof(age=-timedelta(hours=1))
    _purchase_id, result = await quote_with(reap, tarte_quote(30.0), our_price=3200)
    assert result.last_error_code == "quote_items_subtotal_mismatch"


async def test_a_subtotal_that_is_not_a_multiple_of_the_quantity_refuses_and_records_it(
        reap, monkeypatch):
    corroboration_on(monkeypatch)
    await write_enrichment_proof()
    purchase_id, result = await quote_with(reap, tarte_quote(59.99), our_price=3200, quantity=2)
    assert result.last_error_code == "quote_items_subtotal_mismatch"
    row = await get(purchase_id)
    assert (row["live_unit_price_minor"], row["live_items_subtotal_minor"]) == (None, 5999)
    assert public_body(row)["live_price"]["unit_price_minor"] is None


async def test_quantity_two_corroborates_on_the_unit_price(reap, monkeypatch):
    corroboration_on(monkeypatch)
    await write_enrichment_proof()
    purchase_id, result = await quote_with(reap, tarte_quote(60.0), our_price=3200, quantity=2)
    assert result.state == "awaiting_approval"
    assert (await get(purchase_id))["price_rebound_to_minor"] == 3000


def _broken(kind):
    quote = tarte_quote(30.0)
    breakdown = quote["amountBreakdown"]
    if kind == "currency":
        breakdown["shipping"]["currency"] = "EUR"
        return quote, "quote_currency_mismatch"
    if kind == "unreconciled":
        breakdown["finalAmount"]["amount"] = 40.0
        return quote, "quote_total_not_reconciled"
    if kind == "adjustments":
        breakdown["additionalCharges"] = [{"name": "fee", "amount": {"amount": 1.0, "currency": "USD"}}]
        return quote, "quote_adjustments_unsupported"
    if kind == "shipping":
        quote["shippingOptions"][0]["price"]["amount"] = 7.0
        return quote, "quote_shipping_not_reconciled"
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["currency", "unreconciled", "adjustments", "shipping"])
async def test_corroboration_never_overrides_another_failing_check(reap, monkeypatch, kind):
    """A LOWER, corroborated subtotal on a quote that fails something else: that something else
    is the answer, and nothing is rebound."""
    corroboration_on(monkeypatch)
    await write_enrichment_proof()
    quote, code = _broken(kind)
    purchase_id, result = await quote_with(reap, quote, our_price=3200)
    assert result.state == "refused" and result.last_error_code == code, result
    assert (await get(purchase_id))["price_rebound_to_minor"] is None
    assert reap.named("create_checkout") == []


async def test_the_pilot_total_cap_still_binds_after_a_lower_rebind(reap, monkeypatch):
    """Our price x1 = 3200 is inside the cap (admission), Reap's lower-unit quote TOTAL 3500 is
    not: the cap is enforced on the quote total, so the rebind cannot carry a purchase past it."""
    corroboration_on(monkeypatch)
    monkeypatch.setenv(svc.REAP_AGENTIC_PILOT_SCOPE_ENV, json.dumps({
        "agent_ids": ["agent_one"], "merchant_domains": [TARTE_HOST], "markets": ["US"],
        "product_keys": [TARTE_PK], "quantities": [1], "variant_keys": ["shopify:" + BERRY],
        "currency": "USD", "max_total_minor": 3400}))
    await write_enrichment_proof()
    purchase_id, result = await quote_with(reap, tarte_quote(30.0), our_price=3200)
    assert (result.state, result.last_error_code) == ("refused", "pilot_scope_refused"), result
    assert reap.named("create_checkout") == []


async def test_an_exact_quote_clears_a_stale_live_price(reap, monkeypatch):
    corroboration_on(monkeypatch)
    reap.request_cart_link_quote = ok(tarte_quote(30.0))
    purchase_id = await open_tarte(3000)
    assert (await step(purchase_id)).state == "quoting"
    await database.execute(
        "UPDATE reap_agentic_purchases SET live_items_subtotal_minor = 2800, "
        "live_price_stage = 'preflight' WHERE id = :i", {"i": purchase_id})
    assert (await step(purchase_id)).state == "awaiting_approval"
    assert picture(await get(purchase_id)) == NO_PICTURE


# ── the mirror lane: no recorded currency, no corroboration ──────────────────────────────────

from scripts.backfill_shopify_variant_ids import build_cart_proof  # noqa: E402
from services.shopify_variant_identity import parse_product_js, stamp_variant_ids  # noqa: E402

JUDY_JS = json.loads((FIXTURES / "judydoll_silky_matte_lip_ink_products_js_2026_09_29.json").read_text())
JUDY_SEED = json.loads((FIXTURES / "judydoll_silky_matte_lip_ink_seed_2026_09_29.json").read_text())
JUDY_HOST = "judydoll.com"
JUDY_VARIANT = "49819267301653"  # 07 BURGUNDY INK, $13.99 live
JUDY_JS_URL = "https://judydoll.com/products/silky-matte-lip-ink.js"
JUDY_PK = "prod::external_seed::external_seed::ext_price_witness_judy"
JUDY_SEED_ID = "price_witness_judy_seed"


def backfilled_seed(*, checked_at, currency=None):
    """What the backfill writes into seed_data for this fetch: its own functions, its own proof.
    `currency` adds the key NO writer records today -- the forward contract only."""
    seed = copy.deepcopy(JUDY_SEED["seed_data"])
    live = parse_product_js(JUDY_JS)
    new_variants, _ = stamp_variant_ids(seed["snapshot"]["variants"], live)
    proof = build_cart_proof(seed, new_variants, JUDY_JS, live, js_url=JUDY_JS_URL,
                             page_url=JUDY_SEED["canonical_url"], shop_host=JUDY_HOST,
                             checked_at=checked_at)
    assert proof["price_minor"] == 1399 and "currency" not in proof  # the real shape
    if currency is not None:
        proof["currency"] = currency
    seed["snapshot"].update({"variants": new_variants, "shopify_cart_proof": proof})
    return seed


async def _rename_away(table):
    found = await database.fetch_one(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=:t"
        if IS_POSTGRES else "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t", {"t": table})
    if found:
        await database.execute(f"ALTER TABLE {table} RENAME TO {table}_pw_inherited")
    return bool(found)


@pytest.fixture
async def mirror_tables():
    """Own `catalog_products` + `external_product_seeds` for the test (only the columns the
    reader names), restoring whatever another suite left behind."""
    renamed = {t: await _rename_away(t) for t in ("catalog_products", "external_product_seeds")}
    await database.execute(
        "CREATE TABLE catalog_products (product_key TEXT PRIMARY KEY, source_system TEXT, "
        "source_ref TEXT, suppression_reason TEXT, suppressed_at TEXT)")
    await database.execute(
        "CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, status TEXT, domain TEXT, "
        "market TEXT, destination_url TEXT, canonical_url TEXT, attached_product_key TEXT, "
        "seed_data TEXT)")
    try:
        yield
    finally:
        for table, was in renamed.items():
            await database.execute(f"DROP TABLE IF EXISTS {table}")
            if was:
                await database.execute(f"ALTER TABLE {table}_pw_inherited RENAME TO {table}")


async def seed_mirror(seed_data):
    await database.execute(
        "INSERT INTO catalog_products (product_key, source_system, source_ref) "
        "VALUES (:pk, 'external_product_seeds_mirror_v1', :seed)", {"pk": JUDY_PK, "seed": JUDY_SEED_ID})
    await database.execute(
        "INSERT INTO external_product_seeds (id, status, domain, market, destination_url, "
        "canonical_url, attached_product_key, seed_data) VALUES (:id, 'active', :d, 'US', :dest, "
        ":canon, :pk, :data)",
        {"id": JUDY_SEED_ID, "d": JUDY_HOST, "dest": JUDY_SEED["destination_url"],
         "canon": JUDY_SEED["canonical_url"], "pk": JUDY_PK, "data": json.dumps(seed_data)})


async def judy_quote(reap, our_price):
    quote = tarte_quote(13.99)
    reap.request_cart_link_quote = ok(quote)
    await active_enrollment()
    url = f"https://{JUDY_HOST}/cart/{JUDY_VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=US"
    purchase_id = await start(cart_link=item(cart_url=url, shop_domain=JUDY_HOST,
                                             our_price_minor=our_price, product_key=JUDY_PK))
    assert (await step(purchase_id)).state == "quoting"
    return purchase_id, await step(purchase_id)


@pytest.mark.parametrize("our_price", [1499, 1299], ids=["lower", "higher"])
async def test_a_mirror_proof_without_a_recorded_currency_never_corroborates(
        reap, monkeypatch, mirror_tables, our_price):
    """THE CURRENCY DECISION. The backfill's real proof carries `price_minor` 1399 and no
    currency (products.js has none). Accepting it on the row's or the seed's currency would be an
    assumption, so it refuses as today -- the mutant that drops the currency rule continues."""
    corroboration_on(monkeypatch)
    await seed_mirror(backfilled_seed(checked_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    purchase_id, result = await judy_quote(reap, our_price)
    assert result.last_error_code == "quote_items_subtotal_mismatch", result
    assert (await get(purchase_id))["live_unit_price_minor"] == 1399


async def test_a_mirror_proof_that_records_its_currency_corroborates(reap, monkeypatch, mirror_tables):
    """The forward contract: once the writer records the currency it read, the reader accepts it
    (and the read itself -- product -> seed, both dialects -- is the one exercised here)."""
    corroboration_on(monkeypatch)
    await seed_mirror(backfilled_seed(checked_at=datetime.now(timezone.utc) - timedelta(hours=1),
                                      currency="USD"))
    purchase_id, result = await judy_quote(reap, 1499)
    assert result.state == "awaiting_approval", result
    row = await get(purchase_id)
    assert (row["price_rebound_to_minor"], row["price_corroboration_source"]) == (1399, "mirror_proof")


async def test_a_stale_mirror_proof_does_not_corroborate(reap, monkeypatch, mirror_tables):
    corroboration_on(monkeypatch)
    await seed_mirror(backfilled_seed(checked_at=datetime.now(timezone.utc) - timedelta(hours=80),
                                      currency="USD"))
    _purchase_id, result = await judy_quote(reap, 1499)
    assert result.last_error_code == "quote_items_subtotal_mismatch"


def test_the_corroboration_reader_needs_the_purchases_own_cart_variant():
    """The variant lane names no storefront variant, and a cart row whose variant_key disagrees
    with its own URL names none either."""
    base = {"item_source": "cart_link", "cart_url": tarte_url(), "variant_key": "shopify:" + BERRY}
    assert corroboration.purchase_variant_id(base) == BERRY
    assert corroboration.purchase_variant_id({**base, "item_source": "reap_variant"}) is None
    assert corroboration.purchase_variant_id({**base, "variant_key": "shopify:1"}) is None


# ══ 3. Part 2: the buy-intent preflight in 'resolving' ═══════════════════════════════════════


def preflight(monkeypatch, mode):
    monkeypatch.setenv(svc.REAP_AGENTIC_PREFLIGHT_MODE_ENV, mode)


async def test_shadow_records_a_price_change_and_still_sends_the_card_page(reap, monkeypatch):
    preflight(monkeypatch, "shadow")
    reap.request_cart_link_quote = ok(tarte_quote(30.0, quote_id="q_witness"))
    purchase_id = await open_tarte(2800, enrolled=False)
    result = await step(purchase_id)
    assert result.state == "needs_enrollment", result
    assert reap.sequence() == ["request_cart_link_quote", "create_enrollment"]
    row = await get(purchase_id)
    assert (row["preflight_outcome"], row["preflight_error_code"]) == (
        "price_changed", "quote_items_subtotal_mismatch")
    assert (row["live_unit_price_minor"], row["live_price_stage"]) == (3000, "preflight")
    assert row["reap_quote_id"] is None  # the witness id is never stored
    (sent,) = reap.named("request_cart_link_quote")
    assert sent["idempotency_extra"] == {"pivotaWitness": "preflight", "purchaseId": purchase_id}
    assert sent["timeout_seconds"] == rc._QUOTE_TIMEOUT_S
    body = public_body(row)
    assert "live_price" not in body and "preflight" not in body  # still in flight


async def test_shadow_ok_exposes_the_confirmed_totals_before_the_card_page(reap, monkeypatch):
    preflight(monkeypatch, "shadow")
    reap.request_cart_link_quote = ok(tarte_quote(30.0))
    purchase_id = await open_tarte(3000, enrolled=False)
    assert (await step(purchase_id)).state == "needs_enrollment"
    row = await get(purchase_id)
    assert row["preflight_outcome"] == "ok"
    body = public_body(row)
    assert body["state"] == "needs_enrollment"
    assert body["preflight"] == {"checked_at": row["preflight_checked_at"], "totals": {
        "currency": "USD", "items_subtotal_minor": 3000, "shipping_minor": 500, "tax_minor": 0,
        "tax_included": False, "total_minor": 3500}}


async def test_enforce_refuses_a_definitive_mismatch_before_any_enrollment(reap, monkeypatch):
    preflight(monkeypatch, "enforce")
    reap.request_cart_link_quote = ok(tarte_quote(30.0))
    purchase_id = await open_tarte(2800, enrolled=False)
    result = await step(purchase_id)
    assert (result.state, result.refusal_reason, result.last_error_code) == (
        "refused", "price_changed", "quote_items_subtotal_mismatch")
    assert reap.sequence() == ["request_cart_link_quote"]
    row = await get(purchase_id)
    assert row["hosted_url"] is None and row["enrollment_id"] is None
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_enrollments") == 0
    assert row["buyer_email"] is None  # terminal: PII gone as on every refusal
    assert public_body(row)["live_price"] == {
        "currency": "USD", "unit_price_minor": 3000, "items_subtotal_minor": 3000,
        "quoted_total_minor": 3500, "stage": "preflight"}


async def test_enforce_refuses_a_corroborated_increase_with_its_own_code(reap, monkeypatch):
    preflight(monkeypatch, "enforce")
    corroboration_on(monkeypatch)
    await write_enrichment_proof()
    reap.request_cart_link_quote = ok(tarte_quote(30.0))
    purchase_id = await open_tarte(2800, enrolled=False)
    result = await step(purchase_id)
    assert (result.state, result.last_error_code) == ("refused", "quote_price_increased_corroborated")
    assert reap.named("create_enrollment") == []


async def test_enforce_continues_on_a_corroborated_lower_price_and_records_it(reap, monkeypatch):
    preflight(monkeypatch, "enforce")
    corroboration_on(monkeypatch)
    await write_enrichment_proof()
    reap.request_cart_link_quote = ok(tarte_quote(30.0))
    purchase_id = await open_tarte(3200, enrolled=False)
    assert (await step(purchase_id)).state == "needs_enrollment"
    row = await get(purchase_id)
    assert row["preflight_outcome"] == "ok" and row["preflight_total_minor"] == 3500
    assert (row["price_rebound_from_minor"], row["price_rebound_to_minor"]) == (3200, 3000)
    assert row["our_price_minor"] == 3200


def _rejected(code, status=400):
    return rc.ReapResponse(ok=False, status=status, error=f"reap_status_{status}", error_code=code)


@pytest.mark.parametrize("answer,code", [
    (rc.ReapResponse(ok=False, error="transport_error:ReadTimeout"), "transport_error:readtimeout"),
    (rc.ReapResponse(ok=False, status=429, error="reap_status_429"), "reap_status_429"),
    (rc.ReapResponse(ok=False, status=503, error="reap_status_503"), "reap_status_503"),
    (_rejected("QUOTE_TEMPORARILY_UNAVAILABLE", 503), "quote_temporarily_unavailable"),
    (_rejected("OFFER_CODE_INVALID"), "offer_code_invalid"),
    (ok(tarte_quote(30.0, amountBreakdown={})), "quote_amounts_unreadable"),
], ids=["timeout", "429", "5xx", "temporarily_unavailable", "offer_code", "unreadable"])
async def test_enforce_continues_on_an_unknown(reap, monkeypatch, answer, code):
    """Unknown is not a mismatch: recorded `unverified`, and the card page is still offered."""
    preflight(monkeypatch, "enforce")
    reap.request_cart_link_quote = answer
    purchase_id = await open_tarte(2800, enrolled=False)
    assert (await step(purchase_id)).state == "needs_enrollment"
    row = await get(purchase_id)
    assert (row["preflight_outcome"], row["preflight_error_code"]) == ("unverified", code)
    assert reap.sequence().count("request_cart_link_quote") == 1  # no tight retry


@pytest.mark.parametrize("code,reason", [
    ("VARIANT_UNAVAILABLE", "variant_unavailable"),
    ("QUOTE_UNFULFILLABLE", "quote_unfulfillable"),
    ("CARD_PAYMENT_UNAVAILABLE", "card_payment_unavailable"),
])
async def test_enforce_refuses_an_unpurchasable_item_before_enrollment(reap, monkeypatch, code, reason):
    preflight(monkeypatch, "enforce")
    reap.request_cart_link_quote = _rejected(code, 422 if code == "QUOTE_UNFULFILLABLE" else 400)
    purchase_id = await open_tarte(3000, enrolled=False)
    result = await step(purchase_id)
    assert (result.state, result.refusal_reason) == ("refused", reason), result
    assert reap.named("create_enrollment") == []


async def test_no_shipping_option_is_definitive_at_the_preflight(reap, monkeypatch):
    preflight(monkeypatch, "enforce")
    reap.request_cart_link_quote = ok(tarte_quote(30.0, shippingOptions=[]))
    purchase_id = await open_tarte(3000, enrolled=False)
    result = await step(purchase_id)
    assert (result.state, result.refusal_reason, result.last_error_code) == (
        "refused", "no_shipping_option", "quote_no_shipping_option")


async def test_a_retry_never_quotes_a_second_time(reap, monkeypatch):
    """The witness is recorded, then the enrollment create fails transiently (released). The next
    tick goes straight to the enrollment: one witness per attempt."""
    preflight(monkeypatch, "shadow")
    reap.request_cart_link_quote = ok(tarte_quote(30.0))
    reap.create_enrollment = [rc.ReapResponse(ok=False, error="transport_error:ConnectError"),
                              ok(cart_enrollment_created())]
    purchase_id = await open_tarte(3000, enrolled=False)
    assert (await step(purchase_id)).outcome == "released"
    await database.execute("UPDATE reap_agentic_purchases SET next_poll_at = CURRENT_TIMESTAMP WHERE id = :i",
                           {"i": purchase_id})
    assert (await step(purchase_id)).state == "needs_enrollment"
    assert reap.sequence().count("request_cart_link_quote") == 1
    assert reap.sequence().count("create_enrollment") == 2


def cart_enrollment_created():
    from reap_cart_link_cases import ENROLLMENT_CREATED

    return ENROLLMENT_CREATED


async def test_enforce_reapplies_a_recorded_refusal_without_quoting_again(reap, monkeypatch):
    """A lease lost between the record and the refusal: the next tick refuses from the record."""
    preflight(monkeypatch, "enforce")
    purchase_id = await open_tarte(2800, enrolled=False)
    await database.execute(
        "UPDATE reap_agentic_purchases SET preflight_outcome = 'price_changed', "
        "preflight_error_code = 'quote_items_subtotal_mismatch' WHERE id = :i", {"i": purchase_id})
    result = await step(purchase_id)
    assert (result.state, result.refusal_reason) == ("refused", "price_changed")
    assert reap.sequence() == []


async def test_an_interrupted_witness_is_unverified_and_never_resent(reap, monkeypatch):
    preflight(monkeypatch, "enforce")
    purchase_id = await open_tarte(2800, enrolled=False)
    await database.execute("UPDATE reap_agentic_purchases SET preflight_outcome = 'pending' WHERE id = :i",
                           {"i": purchase_id})
    assert (await step(purchase_id)).state == "needs_enrollment"
    row = await get(purchase_id)
    assert (row["preflight_outcome"], row["preflight_error_code"]) == ("unverified", "preflight_interrupted")
    assert reap.sequence() == ["create_enrollment"]


async def test_a_lost_claim_takes_no_witness(reap, monkeypatch):
    preflight(monkeypatch, "shadow")
    purchase_id = await open_tarte(3000, enrolled=False)
    await database.execute(
        "UPDATE reap_agentic_purchases SET claimed_by = 'w1', claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": purchase_id})
    row = await ledger.get_purchase_internal(purchase_id)
    await database.execute("UPDATE reap_agentic_purchases SET claimed_by = 'w2' WHERE id = :i",
                           {"i": purchase_id})
    assert await witness.begin_preflight(row, "w1") is False
    assert (await get(purchase_id))["preflight_outcome"] is None


async def test_the_witness_can_begin_only_once_per_attempt(reap, monkeypatch):
    """The fence itself: the same holder, the same claim, a second begin is refused -- so two
    coroutines of one worker (or a replayed step) cannot both send a witness."""
    purchase_id = await open_tarte(3000, enrolled=False)
    await database.execute(
        "UPDATE reap_agentic_purchases SET claimed_by = 'w1', claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": purchase_id})
    row = await ledger.get_purchase_internal(purchase_id)
    assert await witness.begin_preflight(row, "w1") is True
    assert await witness.begin_preflight(row, "w1") is False
    assert (await get(purchase_id))["preflight_outcome"] == "pending"


async def test_a_local_stop_withdraws_the_marker_and_pauses(reap, monkeypatch):
    """The create pause raised at the client's dispatch boundary: nothing was sent, the marker is
    withdrawn, and a later tick takes the witness."""
    preflight(monkeypatch, "shadow")

    def _stopped(**kwargs):
        raise rc.ProviderOperationStopped()

    reap.request_cart_link_quote = [_stopped, ok(tarte_quote(30.0))]
    purchase_id = await open_tarte(3000, enrolled=False)
    result = await step(purchase_id)
    assert result.outcome == "released" and result.state == "resolving", result
    assert (await get(purchase_id))["preflight_outcome"] is None
    await database.execute("UPDATE reap_agentic_purchases SET next_poll_at = CURRENT_TIMESTAMP WHERE id = :i",
                           {"i": purchase_id})
    assert (await step(purchase_id)).state == "needs_enrollment"
    assert (await get(purchase_id))["preflight_outcome"] == "ok"


@pytest.mark.parametrize("dial,value", [("REAP_AGENTIC_CREATE_ENABLED", "0"),
                                        ("REAP_AGENTIC_RECONCILE_ENABLED", "0")])
async def test_the_create_pause_and_the_reconciliation_stop_take_no_witness(reap, monkeypatch, dial, value):
    preflight(monkeypatch, "enforce")
    purchase_id = await open_tarte(2800, enrolled=False)
    monkeypatch.setenv(dial, value)
    result = await step(purchase_id)
    assert result.outcome == "released" and result.state == "resolving"
    assert reap.sequence() == []
    assert (await get(purchase_id))["preflight_outcome"] is None


async def test_the_witness_quote_id_never_reaches_a_checkout(reap, monkeypatch):
    """Preflight in 'resolving' (q_witness), the buyer enrols, the approval quote (q_cart_1) is
    what is checked out -- and only the witness carried the separate idempotency material."""
    preflight(monkeypatch, "enforce")
    reap.request_cart_link_quote = [ok(tarte_quote(30.0, quote_id="q_witness")),
                                    ok(tarte_quote(30.0, quote_id="q_cart_1"))]
    purchase_id = await open_tarte(3000, enrolled=False)
    assert (await step(purchase_id)).state == "needs_enrollment"
    assert (await step(purchase_id)).state == "quoting"  # get_enrollment answers ACTIVE
    assert (await step(purchase_id)).state == "awaiting_approval"
    (checkout,) = reap.named("create_checkout")
    assert checkout["quote_id"] == "q_cart_1"
    witness_call, approval_call = reap.named("request_cart_link_quote")
    assert "idempotency_extra" in witness_call and "idempotency_extra" not in approval_call
    assert (await get(purchase_id))["reap_quote_id"] == "q_cart_1"


async def test_an_enrolled_buyer_goes_straight_to_the_approval_quote(reap, monkeypatch):
    """No card page to protect: the approval quote is seconds away and decides."""
    preflight(monkeypatch, "enforce")
    purchase_id = await open_tarte(2800, enrolled=True)
    assert (await step(purchase_id)).state == "quoting"
    assert reap.sequence() == []


async def test_the_variant_lane_witness_sends_the_approval_quotes_body(reap, monkeypatch):
    """The SAME builder as `_step_quoting`: the resolver's variant id, our quantity, the buyer's
    email and address -- and the witness key material on top."""
    preflight(monkeypatch, "shadow")
    reap.resolve_our_row = rc.VariantResolution(
        ok=True, variant_id="var_abc123", product_id="prd_abc123", price=(42.50, "USD"),
        available=True, queries_tried=["q"])
    quote = tarte_quote(42.5, shipping=2.5)
    reap.request_quote = ok(quote)
    from reap_cart_link_cases import ADDRESS, EMAIL, RETURN_URL, CONSENT

    purchase_id = await svc.start_purchase(
        agent_id="agent_one", agent_user_ref_hash="hash_alice", buyer_ref="bref_alice",
        row=svc.PurchaseRow(merchant_domain="brand.example", product_key="pk_1", variant_key="vk_1",
                            product_name="Standard", variant_title="Standard", brand="Brand",
                            category="fragrance", our_price_minor=4250, currency="USD",
                            market_country="US"),
        buyer=svc.BuyerContact(email=EMAIL, shipping_address=dict(ADDRESS)), quantity=1,
        click_id="click_abc", return_url=RETURN_URL, consent_version=CONSENT)
    assert (await step(purchase_id)).state == "needs_enrollment"
    (sent,) = reap.named("request_quote")
    assert sent["items"] == [{"variantId": "var_abc123", "quantity": 1}]
    assert sent["email"] == EMAIL and sent["shipping_address"]["city"] == ADDRESS["city"]
    assert sent["idempotency_extra"]["pivotaWitness"] == "preflight"
    assert (await get(purchase_id))["preflight_outcome"] == "ok"


async def test_the_client_keys_the_witness_apart_and_sends_the_same_body(monkeypatch):
    seen = []

    async def _post(path, body, **kwargs):
        seen.append((path, body, kwargs))
        return rc.ReapResponse(ok=True, status=200, data={})

    monkeypatch.setattr(rc, "_post", _post)
    args = dict(items=[{"variantId": "var_1", "quantity": 1}], email="a@b.example")
    await rc.request_quote(**args)
    await rc.request_quote(**args, idempotency_extra={"pivotaWitness": "preflight"})
    (_, plain_body, plain), (_, witness_body, keyed) = seen
    assert plain_body == witness_body
    assert "idempotency_extra" not in plain and keyed["idempotency_extra"] == {"pivotaWitness": "preflight"}
    plain_key = rc._headers("k", "/agentic/quotes", plain_body)["Idempotency-Key"]
    witness_key = rc._headers("k", "/agentic/quotes", witness_body,
                              idempotency_extra=keyed["idempotency_extra"])["Idempotency-Key"]
    assert plain_key != witness_key


# ══ 4. Part 3: the GET view ══════════════════════════════════════════════════════════════════


def test_the_view_shows_a_live_price_only_on_a_price_changed_refusal():
    base = {"id": "rp_1", "state": "refused", "refusal_reason": "price_changed", "currency": "USD",
            "our_price_minor": 2800, "live_unit_price_minor": 3000, "live_items_subtotal_minor": 3000,
            "live_quoted_total_minor": 3500, "live_price_stage": "approval"}
    assert public_body(base)["live_price"]["unit_price_minor"] == 3000
    for other in ({"state": "quoting"}, {"refusal_reason": "variant_unavailable"},
                  {"state": "failed"}, {"live_items_subtotal_minor": None}):
        assert "live_price" not in public_body({**base, **other}), other
    # never the flat columns
    assert not set(witness.COLUMNS) & set(public_body(base))


def test_the_view_shows_preflight_totals_only_when_confirmed():
    base = {"id": "rp_1", "state": "needs_enrollment", "currency": "USD",
            "preflight_outcome": "ok", "preflight_total_minor": 3500,
            "preflight_items_subtotal_minor": 3000, "preflight_shipping_minor": 500,
            "preflight_tax_minor": 0, "preflight_tax_included": False}
    assert public_body(base)["preflight"]["totals"]["total_minor"] == 3500
    for outcome in ("price_changed", "unverified", "refused", "pending"):
        assert "preflight" not in public_body({**base, "preflight_outcome": outcome})


def test_the_allowlist_carries_every_witness_column_and_nothing_private():
    assert set(witness.COLUMNS) <= set(ledger.PUBLIC_PURCHASE_COLUMNS)
    view = ledger.public_purchase_view({"id": "rp_1", "buyer_email": "x@y.z", **{c: 1 for c in witness.COLUMNS}})
    assert set(view) == {"id"} | set(witness.COLUMNS)


# ══ 5. review round: every corroboration guard, the two-dial boundary, the stale view, budget ═


PINK = "63530896753009"  # sold out in the real fixture


def producer_proof(*, variant=BERRY, js_price=None, available=None, age=timedelta(hours=1),
                   currency="USD"):
    """The proof row the JOB's own `evidence_from_product` -> `decide_proof` builds, as the dict
    the table holds (not written). `available` overrides that ONE variant's storefront flag in
    the `.js` body before the job reads it."""
    checked = datetime.now(timezone.utc) - age
    body = tarte_js(js_price)
    for entry in body["variants"]:
        if str(entry["id"]) == variant and available is not None:
            entry["available"] = available
    evidence = evidence_from_product(body, requested_handle=body["handle"], source=SOURCE_PRODUCTS_JS,
                                     checked_at=checked, currency=currency, currency_problem=None)
    row = decide_proof(target(TARTE, "::v:" + variant), evidence, market_currency=currency)
    assert row.outcome == "ok", row.outcome
    return {k: v for k, v in row.__dict__.items() if k not in ("shopify_product_id", "variant_title",
                                                                "live_variant_count")}


def unit_price(proofs, variant=BERRY, host=TARTE_HOST, currency="USD"):
    return corroboration.enrichment_unit_price(
        proofs, variant_id=variant, shop_host=host, currency=currency,
        now=datetime.now(timezone.utc), max_age=timedelta(hours=72))


def test_control_a_producer_proof_corroborates():
    assert unit_price([producer_proof()]) == 3000


def test_a_sibling_variants_proof_never_corroborates():
    """Pink, made available on the storefront, priced $30.00 like the quote: it is another shade."""
    sibling = producer_proof(variant=PINK, available=True)
    assert sibling["variant_id"] == PINK and sibling["live_price_minor"] == 3000
    assert unit_price([sibling], variant=BERRY) is None


def test_a_sold_out_variant_never_corroborates():
    """The job writes `ok` with available=False and a price for a sold-out shade (real fixture)."""
    sold_out = producer_proof(variant=PINK)
    assert sold_out["available"] is False and sold_out["live_price_minor"] == 3000
    assert unit_price([sold_out], variant=PINK) is None


def test_only_an_ok_outcome_corroborates():
    """The job never writes a price on a refusal; a row that carries one anyway is not evidence."""
    assert unit_price([{**producer_proof(), "outcome": "variant_gone"}]) is None


@pytest.mark.parametrize("host,expected", [
    ("tartecosmetics.com", 3000), ("www.tartecosmetics.com", 3000),
    ("uk.tartecosmetics.com", None), ("eviltartecosmetics.com", None),
])
def test_the_proof_must_be_the_same_storefront(host, expected):
    assert unit_price([{**producer_proof(), "shop_host": host}]) == expected


def test_usable_proofs_must_agree():
    """Two sku spellings of one variant, read at different prices: neither corroborates."""
    a = producer_proof()
    b = {**producer_proof(js_price=2900), "sku_key": a["sku_key"] + "_alias"}
    assert unit_price([a, b]) is None
    assert unit_price([a, {**a, "sku_key": a["sku_key"] + "_alias"}]) == 3000


async def test_the_proof_read_is_scoped_to_the_purchases_variant():
    """The SQL join itself: a sibling's proof row is never handed to the verifier."""
    import db.enrichment_cart_variant_proofs as proofs

    assert await proofs.ensure_table()
    await write_enrichment_proof()
    body = tarte_js()
    for entry in body["variants"]:
        if str(entry["id"]) == PINK:
            entry["available"] = True
    checked = datetime.now(timezone.utc) - timedelta(hours=1)
    evidence = evidence_from_product(body, requested_handle=body["handle"], source=SOURCE_PRODUCTS_JS,
                                     checked_at=checked, currency="USD", currency_problem=None)
    assert await upsert_proof(database, decide_proof(target(TARTE, "::v:" + PINK), evidence,
                                                     market_currency="USD"), written_at=checked)
    rows = await witness.enrichment_proofs_for_variant(TARTE_PK, BERRY)
    assert [r["variant_id"] for r in rows] == [BERRY]


@pytest.mark.parametrize("mirror,expected", [(2900, None), (3000, 3000)])
async def test_both_lanes_must_agree(monkeypatch, mirror, expected):
    await write_enrichment_proof()

    async def _seed(product_key, market):
        return {"seed_data": {}, "canonical_url": "https://tartecosmetics.com/products/x"}

    monkeypatch.setattr(witness, "mirror_seed_for_product", _seed)
    monkeypatch.setattr(corroboration, "mirror_unit_price", lambda *a, **k: mirror)
    row = {"item_source": "cart_link", "cart_url": tarte_url(), "variant_key": "shopify:" + BERRY,
           "product_key": TARTE_PK, "currency": "USD", "merchant_domain": TARTE_HOST,
           "market_country": "US"}
    found = await corroboration.independent_unit_price(
        row, now=datetime.now(timezone.utc), max_age=timedelta(hours=72))
    assert (found.unit_price_minor if found else None) == expected


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_preflight_alone_never_continues_at_a_lower_price(reap, monkeypatch, mode):
    """The two-dial boundary: the witness is armed, corroboration is NOT. A matching proof and a
    lower quote are a price change, not a rebind."""
    preflight(monkeypatch, mode)
    await write_enrichment_proof()
    reap.request_cart_link_quote = ok(tarte_quote(30.0))
    purchase_id = await open_tarte(3200, enrolled=False)
    result = await step(purchase_id)
    row = await get(purchase_id)
    assert row["preflight_outcome"] == "price_changed" and row["price_rebound_to_minor"] is None
    assert result.state == ("refused" if mode == "enforce" else "needs_enrollment")


async def test_preflight_alone_refuses_the_lower_approval_quote(reap, monkeypatch):
    preflight(monkeypatch, "shadow")
    await write_enrichment_proof()
    _purchase_id, result = await quote_with(reap, tarte_quote(30.0), our_price=3200)
    assert (result.state, result.last_error_code) == ("refused", "quote_items_subtotal_mismatch")


async def test_a_later_refusal_never_shows_the_preflights_live_price(reap, monkeypatch):
    """The reviewer's case: the witness saw $30.00 against our $28.00 (shadow, continue); the
    approval quote is at OUR $28.00 but its shipping does not reconcile. The view must not tell the
    buyer "price updated to $30"."""
    preflight(monkeypatch, "shadow")
    bad_shipping = tarte_quote(28.0)
    bad_shipping["shippingOptions"][0]["price"]["amount"] = 7.0
    reap.request_cart_link_quote = [ok(tarte_quote(30.0, quote_id="q_witness")), ok(bad_shipping)]
    purchase_id = await open_tarte(2800, enrolled=False)
    assert (await step(purchase_id)).state == "needs_enrollment"
    assert (await get(purchase_id))["live_unit_price_minor"] == 3000  # precondition
    assert (await step(purchase_id)).state == "quoting"
    result = await step(purchase_id)
    assert (result.state, result.last_error_code) == ("refused", "quote_shipping_not_reconciled")
    row = await get(purchase_id)
    assert picture(row) == NO_PICTURE
    assert "live_price" not in public_body(row)


async def test_a_later_quote_drops_the_preflights_rebind(reap, monkeypatch):
    preflight(monkeypatch, "shadow")
    corroboration_on(monkeypatch)
    await write_enrichment_proof()
    reap.request_cart_link_quote = [ok(tarte_quote(30.0, quote_id="q_witness")),
                                    ok(tarte_quote(32.0))]
    purchase_id = await open_tarte(3200, enrolled=False)
    assert (await step(purchase_id)).state == "needs_enrollment"
    assert (await get(purchase_id))["price_rebound_to_minor"] == 3000  # precondition
    assert (await step(purchase_id)).state == "quoting"
    assert (await step(purchase_id)).state == "awaiting_approval"  # at our own price
    row = await get(purchase_id)
    assert picture(row) == NO_PICTURE and "price_rebound" not in public_body(row)


async def _variant_lane_purchase():
    from reap_cart_link_cases import ADDRESS, EMAIL, RETURN_URL, CONSENT

    return await svc.start_purchase(
        agent_id="agent_one", agent_user_ref_hash="hash_alice", buyer_ref="bref_alice",
        row=svc.PurchaseRow(merchant_domain="brand.example", product_key="pk_1", variant_key="vk_1",
                            product_name="Standard", variant_title="Standard", brand="Brand",
                            category="fragrance", our_price_minor=4250, currency="USD",
                            market_country="US"),
        buyer=svc.BuyerContact(email=EMAIL, shipping_address=dict(ADDRESS)), quantity=1,
        click_id="click_abc", return_url=RETURN_URL, consent_version=CONSENT)


@pytest.mark.parametrize("resolve_seconds,expected_timeout", [(110.0, None), (90.0, 30.0), (10.0, 35.0)])
async def test_the_witness_fits_inside_the_resolving_step_budget(reap, monkeypatch, resolve_seconds,
                                                                 expected_timeout):
    """resolve + witness + enrollment reserve <= 170 s (the lease floor's derivation): a slow
    resolve shortens the witness, and below 22 s left it is not sent at all."""
    preflight(monkeypatch, "enforce")
    clock = [1000.0]
    monkeypatch.setattr(svc, "_monotonic", lambda: clock[0])

    def _resolve(**kwargs):
        clock[0] += resolve_seconds
        return rc.VariantResolution(ok=True, variant_id="var_abc123", product_id="prd_abc123",
                                    price=(42.50, "USD"), available=True, queries_tried=["q"])

    reap.resolve_our_row = _resolve
    reap.request_quote = ok(tarte_quote(42.5, shipping=2.5))
    purchase_id = await _variant_lane_purchase()
    assert (await step(purchase_id)).state == "needs_enrollment"
    sent = reap.named("request_quote")
    row = await get(purchase_id)
    if expected_timeout is None:
        assert sent == [] and (row["preflight_outcome"], row["preflight_error_code"]) == (
            "unverified", "preflight_no_budget")
    else:
        assert [s["timeout_seconds"] for s in sent] == [expected_timeout]
        assert row["preflight_outcome"] == "ok"
    assert svc.RESOLVING_STEP_BUDGET_S == svc.QUOTING_STEP_BUDGET_S == 170.0


def test_only_the_proof_jobs_sources_corroborate():
    assert unit_price([{**producer_proof(), "source": "cart_js"}]) is None


def _mirror(seed, **over):
    kwargs = dict(variant_id=JUDY_VARIANT, product_urls=[JUDY_SEED["canonical_url"]],
                  shop_domain=JUDY_HOST, currency="USD", now=datetime.now(timezone.utc),
                  max_age=timedelta(hours=72))
    kwargs.update(over)
    return corroboration.mirror_unit_price(seed, **kwargs)


def _currency_seed(**proof_over):
    seed = backfilled_seed(checked_at=datetime.now(timezone.utc) - timedelta(hours=1), currency="USD")
    seed["snapshot"]["shopify_cart_proof"].update(proof_over)
    return seed


def test_mirror_control_and_each_guard():
    """The mirror reader's guards one by one, on the backfill's own proof (+ the currency key)."""
    assert _mirror(_currency_seed()) == 1399
    assert _mirror(_currency_seed(), variant_id="49819267170581") is None   # a sibling shade
    assert _mirror(_currency_seed(available=False)) is None
    assert _mirror(_currency_seed(source="cart_js")) is None
    assert _mirror(_currency_seed(), shop_domain="evil.example") is None    # the fetch rule
    assert _mirror(_currency_seed(), currency="EUR") is None
    assert _mirror(_currency_seed(price_minor=0)) is None
    assert _mirror(json.dumps(_currency_seed())) == 1399                     # jsonb as text


@pytest.mark.parametrize("price", [0, -1, True, 30.0, None])
def test_only_a_positive_integer_price_corroborates(price):
    assert unit_price([{**producer_proof(), "live_price_minor": price}]) is None
    seed = _currency_seed(price_minor=price)
    assert _mirror(seed) is None


def test_the_mirror_proofs_must_agree():
    """The backfill's per-variant proof beside its named proof: once both state a price (the
    forward contract), they must state the same one."""
    from scripts.backfill_shopify_variant_ids import build_selected_variant_proofs

    seed = _currency_seed()
    selected = build_selected_variant_proofs(seed["snapshot"]["variants"], JUDY_JS, js_url=JUDY_JS_URL,
                                             checked_at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert set(selected) == {JUDY_VARIANT}, selected  # the real producer's shape
    for price, expected in ((1499, None), (1399, 1399)):
        seed["snapshot"]["shopify_cart_variant_proofs"] = {
            JUDY_VARIANT: {**selected[JUDY_VARIANT], "price_minor": price, "currency": "USD"}}
        assert _mirror(seed) == expected
