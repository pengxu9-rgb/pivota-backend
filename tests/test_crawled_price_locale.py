"""A crawled price is read with the page's decimal separator, and its currency is never invented.

THE DEFECT (review of #2416, 2026-09-28). `services.external_offers_service._parse_price` kept
digits and dots and dropped everything else, commas included:

    "28,80"      -> 2880       (100x: every EU/SE comma-decimal price)
    "1.234,56"   -> 1.23456    (1/1000x)
    "2 400,00 €" -> 240000     (100x)

and `resolve_external_offer` wrote `"JPY" if market == "JP" else "USD"` for a page that stated no
currency. Both reached `external_product_seeds` through the nightly refresh, and #2416 would
project them onto canonical offers.

The tables drive `utils.crawled_price.parse_crawled_price` directly; the page tests drive the real
extractor (`_extract_from_html`) and the real refresh, because the extractor decides WHICH text and
WHICH signals reach the parser and a hand-built input would not show that.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest

from utils.crawled_price import (
    AMBIGUOUS_SEPARATOR,
    EMPTY,
    MALFORMED_GROUPING,
    MULTIPLE_NUMBERS,
    NEGATIVE,
    NO_DIGITS,
    NOT_FINITE,
    PARSED,
    agreed_hint,
    decimal_hint_from_currency,
    decimal_hint_from_locale,
    decimal_hint_from_money_format,
    parse_crawled_price,
    read_currency_code,
)

# --------------------------------------------------------------------------------------------
# The parser, table-driven. (raw, currency, decimal_hint) -> (amount, status)
# --------------------------------------------------------------------------------------------

CASES = [
    # --- the review's cases, with no page signal at all ---
    ("28,80", None, None, 28.8, PARSED),  # old: 2880
    ("1.234,56", None, None, 1234.56, PARSED),  # old: 1.23456
    ("1,234.56", None, None, 1234.56, PARSED),
    ("28.80", None, None, 28.8, PARSED),
    ("¥2,400", None, None, 2400.0, PARSED),  # the glyph says "," is a group separator
    ("2 400,00 €", None, None, 2400.0, PARSED),  # old: 240000
    ("28,80 €", None, None, 28.8, PARSED),  # trailing symbol; old: 2880
    ("28.80€", None, None, 28.8, PARSED),
    ("28,80 EUR", None, None, 28.8, PARSED),  # trailing code
    ("249,00 kr", None, None, 249.0, PARSED),
    ("24,000원", None, None, 24000.0, PARSED),
    # --- other separators ---
    ("2 400,00 €", None, None, 2400.0, PARSED),  # NBSP group
    ("2 400,00 €", None, None, 2400.0, PARSED),  # narrow NBSP group
    ("1'234.56", None, None, 1234.56, PARSED),  # CH apostrophe group
    ("12.345.678,90", None, None, 12345678.9, PARSED),
    ("1,234,567", None, None, 1234567.0, PARSED),
    ("1.234.567", None, None, 1234567.0, PARSED),
    ("₹1,23,456.00", None, None, 123456.0, PARSED),  # Indian grouping
    ("28", None, None, 28.0, PARSED),
    ("28.", None, None, 28.0, PARSED),
    (".99", None, None, 0.99, PARSED),
    ("0,5", None, None, 0.5, PARSED),
    ("1234,5", None, None, 1234.5, PARSED),
    # --- THE ambiguous shape: one separator, exactly three digits after it ---
    ("1,234", None, None, None, AMBIGUOUS_SEPARATOR),
    ("1.234", None, None, None, AMBIGUOUS_SEPARATOR),
    ("2.400", None, None, None, AMBIGUOUS_SEPARATOR),
    ("€1.500", None, None, None, AMBIGUOUS_SEPARATOR),  # "€" says nothing
    ("$1.500", None, None, None, AMBIGUOUS_SEPARATOR),  # "$" says nothing
    # ...decided by a zero-decimal currency: a separator there is only ever a group
    ("2.400", "JPY", None, 2400.0, PARSED),
    ("2,400", "JPY", None, 2400.0, PARSED),
    ("24.000", "KRW", ",", 24000.0, PARSED),
    # ...decided by the page's hint, when it names the OTHER separator as the decimal
    ("1,234", "USD", ".", 1234.0, PARSED),
    ("1.234", "EUR", ",", 1234.0, PARSED),
    # ...refused when the hint names THIS separator as the decimal: three decimals is no price
    ("1.234", "USD", ".", None, AMBIGUOUS_SEPARATOR),
    ("1,234", "EUR", ",", None, AMBIGUOUS_SEPARATOR),
    # ...unless the currency really has three decimals, or the third decimal is padding
    ("1.234", "KWD", ".", 1.234, PARSED),
    ("28.000", "USD", ".", 28.0, PARSED),  # a USD page's zero-padded price
    ("1.230", "USD", ".", 1.23, PARSED),
    ("1,000", "EUR", ",", 1.0, PARSED),
    ("28.000", None, None, None, AMBIGUOUS_SEPARATOR),  # padding or thousands: no hint, no read
    # a glyph and the page hint disagree -> no agreed signal -> refused
    ("¥2,400", "CNY", ",", None, AMBIGUOUS_SEPARATOR),
    # the structure wins over a hint when the structure decides
    ("28,80", "USD", ".", 28.8, PARSED),
    ("28.80", "EUR", ",", 28.8, PARSED),
    ("1,234.56", "EUR", ",", 1234.56, PARSED),
    # --- JSON numbers are already unambiguous ---
    (1.234, None, None, 1.234, PARSED),
    (2400, "JPY", None, 2400.0, PARSED),
    (28.8, "EUR", ",", 28.8, PARSED),
    (0, None, None, 0.0, PARSED),
    (-5, None, None, None, NEGATIVE),
    (float("nan"), None, None, None, NOT_FINITE),
    (float("inf"), None, None, None, NOT_FINITE),
    (True, None, None, None, NO_DIGITS),
    # --- refusals ---
    ("", None, None, None, EMPTY),
    (None, None, None, None, EMPTY),
    ("n/a", None, None, None, NO_DIGITS),
    ("€", None, None, None, NO_DIGITS),
    ("28,80 € / 100 ml", None, None, None, MULTIPLE_NUMBERS),
    ("$22.40 $28.00", None, None, None, MULTIPLE_NUMBERS),
    ("28,80 - 35,00", None, None, None, MULTIPLE_NUMBERS),
    ("-5.00", None, None, None, NEGATIVE),
    ("1.234.56", None, None, None, MALFORMED_GROUPING),
    ("1,23.4", None, None, None, MALFORMED_GROUPING),
    ("12,34", None, ".", 12.34, PARSED),  # tail 2 is decimal whatever the hint says
    ("1,2345,678", None, None, None, MALFORMED_GROUPING),
    ("1.2,3", None, None, None, MALFORMED_GROUPING),
]


@pytest.mark.parametrize("raw, currency, hint, amount, status", CASES)
def test_parse_crawled_price(raw: Any, currency: Optional[str], hint: Optional[str], amount, status) -> None:
    read = parse_crawled_price(raw, currency=currency, decimal_hint=hint)
    assert read.status == status, (raw, read)
    if amount is None:
        assert read.amount is None, (raw, read)
    else:
        assert read.amount == pytest.approx(amount), (raw, read)


def test_a_refusal_never_carries_an_amount() -> None:
    for raw, currency, hint, _amount, _status in CASES:
        read = parse_crawled_price(raw, currency=currency, decimal_hint=hint)
        assert (read.amount is None) == (read.status != PARSED), (raw, read)


# --------------------------------------------------------------------------------------------
# The signals
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tag, hint",
    [
        ("de-DE", ","), ("de_DE", ","), ("fr", ","), ("sv-SE", ","), ("nl-NL", ","), ("pt-BR", ","),
        ("en-US", "."), ("en", "."), ("ja-JP", "."), ("ko", "."), ("zh-CN", "."),
        ("de-CH", "."), ("fr-CH", "."), ("es-MX", "."),
        ("", None), (None, None), ("xx", None),
    ],
)
def test_decimal_hint_from_locale(tag, hint) -> None:
    assert decimal_hint_from_locale(tag) == hint


@pytest.mark.parametrize(
    "money_format, hint",
    [
        ("€{{amount_with_comma_separator}}", ","),
        ("{{amount_with_comma_separator}} €", ","),
        ("{{ amount_no_decimals_with_comma_separator }} kr", ","),
        ("{{amount_with_space_separator}} €", ","),
        ("${{amount}}", "."),
        ("£{{amount}}", "."),
        ("¥{{amount_no_decimals}}", "."),
        ("CHF {{amount_with_apostrophe_separator}}", "."),
        ("{{amount}} / {{amount_with_comma_separator}}", None),  # disagree -> no signal
        ("€{{price}}", None),
        ("", None),
    ],
)
def test_decimal_hint_from_money_format(money_format, hint) -> None:
    assert decimal_hint_from_money_format(money_format) == hint


def test_currency_hints_leave_eur_and_cad_undecided() -> None:
    assert decimal_hint_from_currency("USD") == "."
    assert decimal_hint_from_currency("sek") == ","
    # Written both ways (en-IE / de-DE, en-CA / fr-CA): the currency alone decides nothing.
    assert decimal_hint_from_currency("EUR") is None
    assert decimal_hint_from_currency("CAD") is None
    assert decimal_hint_from_currency(None) is None


def test_signals_that_disagree_are_no_signal() -> None:
    assert agreed_hint([",", None, ","]) == ","
    assert agreed_hint([",", "."]) is None
    assert agreed_hint([None, None]) is None


@pytest.mark.parametrize(
    "raw, code",
    [("usd", "USD"), (" EUR ", "EUR"), ("€", "EUR"), ("£", "GBP"), ("₩", "KRW"),
     ("$", None), ("¥", None), ("EURO", None), ("", None), (None, None)],
)
def test_read_currency_code_refuses_ambiguous_symbols(raw, code) -> None:
    assert read_currency_code(raw) == code


# --------------------------------------------------------------------------------------------
# Through the real extractor
# --------------------------------------------------------------------------------------------

URL = "https://beaute.example.de/products/serum"


def _page(
    *,
    lang: Optional[str] = None,
    og_price: Optional[str] = None,
    og_currency: Optional[str] = None,
    og_locale: Optional[str] = None,
    jsonld: Optional[Dict[str, Any]] = None,
    money_format: Optional[str] = None,
    data_attr: Optional[list] = None,
) -> str:
    head = ["<title>Serum</title>", '<meta property="og:title" content="Serum">']
    if og_price is not None:
        head.append(f'<meta property="product:price:amount" content="{og_price}">')
    if og_currency is not None:
        head.append(f'<meta property="product:price:currency" content="{og_currency}">')
    if og_locale is not None:
        head.append(f'<meta property="og:locale" content="{og_locale}">')
    if jsonld is not None:
        head.append(f'<script type="application/ld+json">{json.dumps(jsonld)}</script>')
    if money_format is not None:
        # How Shopify themes expose it: an inline script, JSON-escaped.
        head.append("<script>window.theme = {\"moneyFormat\": " + json.dumps(money_format) + "};</script>")
    body = ""
    if data_attr is not None:
        import html as html_lib

        body = f'<div data-product-skus-value="{html_lib.escape(json.dumps(data_attr))}"></div>'
    lang_attr = f' lang="{lang}"' if lang else ""
    return f"<html{lang_attr}><head>{''.join(head)}</head><body>{body}</body></html>"


def _product(price: Any, currency: Optional[str] = "EUR", **offer: Any) -> Dict[str, Any]:
    o: Dict[str, Any] = {"@type": "Offer", "price": price, "availability": "https://schema.org/InStock", **offer}
    if currency:
        o["priceCurrency"] = currency
    return {"@context": "https://schema.org", "@type": "Product", "name": "Serum", "offers": o}


def _extract(html: str) -> Dict[str, Any]:
    from services.external_offers_service import _extract_from_html

    return _extract_from_html(URL, html)


def test_an_eu_shopify_meta_price_reads_as_euros_not_cents() -> None:
    """EU Shopify themes print `money_without_currency` into product:price:amount."""
    out = _extract(_page(lang="de", og_price="28,80", og_currency="EUR", money_format="{{amount_with_comma_separator}} €"))
    assert out["price_amount"] == pytest.approx(28.8)
    assert out["price_currency"] == "EUR"
    assert out["price_read"]["source"] == "meta"
    assert out["price_read"]["raw"] == "28,80"
    assert out["price_read"]["status"] == PARSED


def test_a_thousands_dot_meta_price_is_not_divided_by_a_thousand() -> None:
    out = _extract(_page(og_price="1.234,56", og_currency="EUR"))
    assert out["price_amount"] == pytest.approx(1234.56)


def test_the_page_locale_decides_an_ambiguous_price() -> None:
    out = _extract(_page(lang="de-DE", og_price="1.234", og_currency="EUR"))
    assert out["price_amount"] == pytest.approx(1234.0)
    assert out["price_read"]["decimal_hint"] == ","


def test_the_shopify_money_format_decides_an_ambiguous_price() -> None:
    out = _extract(_page(og_price="1.234", og_currency="EUR", money_format="€{{amount_with_comma_separator}}"))
    assert out["price_amount"] == pytest.approx(1234.0)


def test_an_unambiguous_currency_is_a_signal_on_its_own() -> None:
    assert _extract(_page(og_price="1,234", og_currency="USD"))["price_amount"] == pytest.approx(1234.0)
    assert _extract(_page(og_price="1.234", og_currency="SEK"))["price_amount"] == pytest.approx(1234.0)
    # EUR says nothing, so the same text is refused
    assert _extract(_page(og_price="1,234", og_currency="EUR"))["price_amount"] is None


def test_og_locale_is_a_signal_too() -> None:
    out = _extract(_page(og_locale="fr_FR", og_price="2.400", og_currency="EUR"))
    assert out["price_amount"] == pytest.approx(2400.0)


def test_an_ambiguous_price_with_no_signal_is_refused_and_says_why() -> None:
    out = _extract(_page(og_price="1.234", og_currency="EUR"))
    assert out["price_amount"] is None
    assert out["price_read"]["status"] == AMBIGUOUS_SEPARATOR
    assert out["price_read"]["raw"] == "1.234"


def test_signals_that_disagree_refuse_an_ambiguous_price() -> None:
    """An `en` theme on a comma-formatting shop: precedence would be a guess."""
    out = _extract(_page(lang="en", og_price="1.234", og_currency="EUR", money_format="{{amount_with_comma_separator}}"))
    assert out["price_amount"] is None
    assert out["price_read"]["status"] == AMBIGUOUS_SEPARATOR


def test_a_json_ld_number_is_not_re_read_as_text() -> None:
    """1.234 as a JSON number is unambiguous; stringified it would look like "1.234"."""
    out = _extract(_page(jsonld=_product(1.234, "KWD")))
    assert out["price_amount"] == pytest.approx(1.234)
    out = _extract(_page(lang="de", jsonld=_product(28.8)))
    assert out["price_amount"] == pytest.approx(28.8)
    # the case where it matters: on a `,` page the TEXT "1.234" is refused (spec vs page), the
    # NUMBER 1.234 is simply 1.234
    out = _extract(_page(lang="de", jsonld=_product(1.234)))
    assert out["price_amount"] == pytest.approx(1.234)
    assert _extract(_page(lang="de", jsonld=_product("1.234")))["price_amount"] is None


def test_a_json_ld_comma_price_reads_right_and_so_do_its_variants() -> None:
    out = _extract(_page(jsonld=_product("28,80")))
    assert out["price_amount"] == pytest.approx(28.8)
    assert out["price_read"]["source"] == "jsonld"
    assert [v["price_amount"] for v in out["variants"]] == [pytest.approx(28.8)]
    assert out["variants"][0]["price_exact"] is True
    assert out["variant_census"]["exact_prices"] == [28.8]
    assert out["variant_census"]["price_refused"] == 0


def test_a_refused_variant_price_is_counted_and_not_exact() -> None:
    out = _extract(_page(jsonld=_product("1.234")))
    assert out["variants"][0]["price_amount"] is None
    assert out["variants"][0]["price_exact"] is False
    assert out["variant_census"]["price_refused"] == 1
    assert out["variant_census"]["exact_prices"] == []


def test_the_page_signals_reach_the_variant_prices_too() -> None:
    """The refresh writes per-variant prices from this list, so they need the same signals."""
    # Without the page's `de`, JSON-LD's own "." rule alone would read this as 28.00.
    out = _extract(_page(lang="de", jsonld=_product("28.000")))
    assert out["variants"][0]["price_amount"] is None
    assert out["variant_census"]["price_refused"] == 1
    skus = [{"id": "a", "size": "30 ml", "price_with_currency_code": "1.234 EUR"}]
    out = _extract(_page(lang="de", data_attr=skus))
    assert out["variants"][0]["price_amount"] == pytest.approx(1234.0)


def test_offer_and_meta_currencies_are_read_not_passed_through() -> None:
    offer = _extract(_page(jsonld=_product("28.80", "$")))
    assert offer["variants"][0]["price_currency"] is None
    offer = _extract(_page(jsonld=_product("28.80", "eur")))
    assert offer["variants"][0]["price_currency"] == "EUR"
    meta = _extract(_page(og_price="28.80", og_currency="$"))
    assert meta["price_currency"] is None
    meta = _extract(_page(og_price="28.80", og_currency=" eur "))
    assert meta["price_currency"] == "EUR"


def test_a_minus_sign_before_the_number_is_negative_not_positive() -> None:
    assert parse_crawled_price("\u221228,80").status == NEGATIVE
    assert parse_crawled_price("EUR -28,80").status == NEGATIVE


def test_json_ld_is_read_by_its_spec_and_must_agree_with_the_page() -> None:
    """schema.org: JSON-LD `price` uses "." as the decimal point. On a `,`-locale page the spec
    and the page disagree about "28.000" (28.00 by the spec, 28000 by the page), so it is refused;
    reading it by the page alone would put a 28 EUR serum at 28000."""
    de = _extract(_page(lang="de", jsonld=_product("28.000")))
    assert de["price_amount"] is None
    assert de["price_read"]["status"] == AMBIGUOUS_SEPARATOR
    us = _extract(_page(lang="en", jsonld=_product("28.000", "USD")))
    assert us["price_amount"] == pytest.approx(28.0)
    # a meta tag carries the shop's own format, so the page's locale alone decides it
    meta = _extract(_page(lang="de", og_price="1.234", og_currency="EUR"))
    assert meta["price_amount"] == pytest.approx(1234.0)
    # structure still wins for JSON-LD text that breaks the spec unambiguously
    assert _extract(_page(lang="de", jsonld=_product("28,80")))["price_amount"] == pytest.approx(28.8)


def test_a_refused_json_ld_price_falls_back_to_the_meta_tag() -> None:
    out = _extract(_page(jsonld=_product("1.234"), og_price="1.234,00", og_currency="EUR"))
    assert out["price_amount"] == pytest.approx(1234.0)
    assert out["price_read"]["source"] == "meta"


def test_a_jp_page_reads_yen_with_group_commas() -> None:
    out = _extract(_page(lang="ja", jsonld=_product("¥2,400", "JPY")))
    assert out["price_amount"] == pytest.approx(2400.0)
    assert out["price_currency"] == "JPY"


def test_data_attr_variants_read_the_currency_suffixed_text() -> None:
    skus = [
        {"id": "a", "size": "30 ml", "price_with_currency_code": "28,80 EUR"},
        {"id": "b", "size": "50 ml", "price_with_currency_code": "1.234,50 EUR"},
    ]
    out = _extract(_page(data_attr=skus))
    by_id = {v["variant_id"]: v for v in out["variants"]}
    assert by_id["a"]["price_amount"] == pytest.approx(28.8)
    assert by_id["b"]["price_amount"] == pytest.approx(1234.5)
    assert by_id["a"]["price_currency"] == "EUR"


def test_a_bare_dollar_sign_is_not_usd() -> None:
    from services.external_offers_service import _detect_currency_from_text

    assert _detect_currency_from_text("$28.00") is None
    assert _detect_currency_from_text("28.00 USD") == "USD"
    assert _detect_currency_from_text("€28,00") == "EUR"


def test_a_symbol_currency_in_json_ld_is_not_passed_through() -> None:
    out = _extract(_page(jsonld=_product("28.80", "$")))
    assert out["price_currency"] is None


# --------------------------------------------------------------------------------------------
# resolve_external_offer: an unread currency is not the market's
# --------------------------------------------------------------------------------------------

def _resolve(monkeypatch: pytest.MonkeyPatch, html: str, market: str) -> Any:
    import services.external_offers_service as svc

    written: Dict[str, Any] = {}

    async def fake_fetch_html(url, **kwargs):
        return html, "text/html"

    async def fake_get_row(market_norm, url_hash):
        if not written:
            return None
        return {**written, "last_checked_at": None}

    async def fake_execute(stmt, values=None):
        written.update(values or {})

    monkeypatch.setattr(svc, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(svc, "_get_snapshot_row", fake_get_row)
    monkeypatch.setattr(svc.database, "execute", fake_execute)
    snap = asyncio.run(svc.resolve_external_offer(market=market, url=URL, force_refresh=True))
    return snap, written


@pytest.mark.parametrize("market", ["US", "JP", "FR"])
def test_resolve_never_invents_a_currency(monkeypatch: pytest.MonkeyPatch, market: str) -> None:
    snap, written = _resolve(monkeypatch, _page(og_price="28.80"), market)
    assert written["price_currency"] is None
    assert written["price_amount"] is None, "an amount without its currency is not a price"
    assert snap.price_currency is None and snap.price_amount is None
    assert written["evidence"]["price_read"]["status"] == "currency_unread"
    assert written["evidence"]["price_read"]["raw"] == "28.80"
    assert written["evidence"]["price_currency_source"] == "unread"
    assert snap.to_public()["price"] is None


def test_resolve_stores_a_read_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    snap, written = _resolve(monkeypatch, _page(lang="de", og_price="28,80", og_currency="EUR"), "FR")
    assert (written["price_amount"], written["price_currency"]) == (pytest.approx(28.8), "EUR")
    assert written["evidence"]["price_read"]["status"] == PARSED
    assert written["evidence"]["price_currency_source"] == "page"
    assert snap.to_public()["price"] == {"amount": pytest.approx(28.8), "currency": "EUR"}


def test_resolve_records_an_ambiguous_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _snap, written = _resolve(monkeypatch, _page(og_price="1.234", og_currency="EUR"), "FR")
    assert written["price_amount"] is None
    assert written["price_currency"] == "EUR"
    assert written["evidence"]["price_read"]["status"] == AMBIGUOUS_SEPARATOR


# --------------------------------------------------------------------------------------------
# The seed refresh: unreadable is no update, and it is counted
# --------------------------------------------------------------------------------------------

def _seed_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "eps_eu_1",
        "external_product_id": "ext_eu_1",
        "market": "FR",
        "tool": "*",
        "destination_url": URL,
        "canonical_url": URL,
        "domain": "beaute.example.de",
        "title": "Serum",
        "image_url": None,
        "price_amount": 28.8,
        "price_currency": "EUR",
        "availability": "in_stock",
        "seed_data": {"title": "Serum", "snapshot": {}},
        "status": "active",
        "attached_product_key": None,
        "attached_variant_id": None,
    }
    row.update(overrides)
    return row


def _snapshot_of(html: str) -> SimpleNamespace:
    """What `resolve_external_offer` hands the refresh, built by the producer's own functions."""
    from services.external_offers_service import evidence_variant_fields, snapshot_price_fields

    extracted = _extract(html)
    amount, currency, price_read = snapshot_price_fields(extracted)
    return SimpleNamespace(
        canonical_url=URL,
        domain="beaute.example.de",
        title=extracted.get("title"),
        image_url=extracted.get("image_url"),
        price_amount=amount,
        price_currency=currency,
        availability=extracted.get("availability") or "unknown",
        last_checked_at=None,
        evidence={"provider": extracted.get("evidence_provider"), **evidence_variant_fields(extracted), "price_read": price_read},
    )


def _refresh(monkeypatch: pytest.MonkeyPatch, row: Dict[str, Any], snapshot: SimpleNamespace) -> Dict[str, Any]:
    import routes.employee_products as mod

    async def fake_fetch_one(_query: str, values=None):
        return row if values and values.get("id") == row["id"] else None

    async def fake_execute_seed_data_stmt(_query: str, values):
        row.update(values)

    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(mod, "resolve_external_offer", AsyncMock(return_value=snapshot))
    return asyncio.run(mod._refresh_external_seed_by_id(row["id"]))


def test_the_refresh_writes_euros_not_cents(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect end to end: a stored 2880 EUR (the old misread) is corrected to 28.80."""
    row = _seed_row(price_amount=2880.0)
    result = _refresh(monkeypatch, row, _snapshot_of(_page(lang="de", og_price="28,80", og_currency="EUR")))
    assert row["price_amount"] == pytest.approx(28.8)
    assert result["price_refresh"]["status"] == "applied"


def test_an_ambiguous_price_is_no_update_and_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _seed_row(price_amount=12.5)
    result = _refresh(monkeypatch, row, _snapshot_of(_page(og_price="1.234", og_currency="EUR")))
    assert row["price_amount"] == pytest.approx(12.5)
    assert row["price_currency"] == "EUR"
    assert result["price_refresh"]["status"] == "skipped_unreadable"
    assert result["price_refresh"]["reason"] == AMBIGUOUS_SEPARATOR
    assert result["price_refresh"]["changed"] is False


def test_an_unread_currency_is_no_update_even_when_the_stored_one_is_usd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hole the currency-mismatch guard could not close: stored USD + defaulted USD agreed."""
    row = _seed_row(market="US", price_amount=28.0, price_currency="USD")
    result = _refresh(monkeypatch, row, _snapshot_of(_page(og_price="2880")))
    assert (row["price_amount"], row["price_currency"]) == (pytest.approx(28.0), "USD")
    assert result["price_refresh"]["status"] == "skipped_unreadable"
    assert result["price_refresh"]["reason"] == "currency_unread"


def test_an_unread_currency_does_not_first_fill_a_priceless_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _seed_row(market="US", price_amount=None, price_currency=None)
    result = _refresh(monkeypatch, row, _snapshot_of(_page(og_price="28.80")))
    assert row["price_amount"] is None and row["price_currency"] is None
    assert result["price_refresh"]["status"] == "skipped_unreadable"


def test_a_page_with_no_price_is_still_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _seed_row()
    result = _refresh(monkeypatch, row, _snapshot_of(_page()))
    assert result["price_refresh"]["status"] == "unavailable"
    assert "reason" not in result["price_refresh"]


def test_the_batch_counts_unreadable_prices_by_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.external_referral_readiness as err

    rows = [
        {"status": "success", "price_refresh": {"status": "skipped_unreadable", "reason": AMBIGUOUS_SEPARATOR}},
        {"status": "success", "price_refresh": {"status": "skipped_unreadable", "reason": "currency_unread"}},
        {"status": "success", "price_refresh": {"status": "skipped_unreadable", "reason": "currency_unread"}},
        {"status": "success", "price_refresh": {"status": "unavailable"}},
    ]
    ids = [f"eps_{i}" for i in range(len(rows))]
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids", lambda *a, **k: asyncio.sleep(0, result=ids))
    hosts = {"eps_0": "a.example.de", "eps_1": "b.example.com", "eps_2": "b.example.com", "eps_3": "c.example"}
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda _ids: asyncio.sleep(0, result=hosts))
    scripted = dict(zip(ids, rows))

    async def fake_refresh(seed_id, **kwargs):
        return scripted[seed_id]

    summary = asyncio.run(err.run_external_referral_refresh_batch(refresh_seed_by_id=fake_refresh, limit=10))
    assert summary["price_skipped_unreadable"] == 3
    assert summary["price_unreadable_reasons"] == {AMBIGUOUS_SEPARATOR: 1, "currency_unread": 2}
    assert summary["price_unavailable"] == 1
    assert list(summary["price_unreadable_top_hosts"].items()) == [("b.example.com", 2), ("a.example.de", 1)]


def test_the_job_prints_one_prefixed_price_line(monkeypatch, capsys):
    """Night one must be observable: the summary dump is multi-line and module INFO is dropped
    in prod, so the price outcomes get one `PRICE_REFRESH {json}` line (textPayload)."""
    import jobs.external_referral_refresh as job

    summary = {
        "status": "success", "price_changed": 4, "price_skipped_unreadable": 3,
        "price_unreadable_reasons": {"currency_unread": 3},
        "price_unreadable_top_hosts": {"b.example.com": 3}, "unrelated": "x",
    }
    monkeypatch.setattr(job, "run_daily_external_referral_refresh", lambda **kwargs: asyncio.sleep(0, result=summary))
    monkeypatch.setattr(job.database, "connect", lambda: asyncio.sleep(0))
    monkeypatch.setattr(job.database, "disconnect", lambda: asyncio.sleep(0))
    monkeypatch.setattr("sys.argv", ["external_referral_refresh", "--limit", "1"])
    assert job.main() == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("PRICE_REFRESH ")]
    assert len(lines) == 1
    line = json.loads(lines[0][len("PRICE_REFRESH "):])
    assert line["price_skipped_unreadable"] == 3
    assert line["price_unreadable_reasons"] == {"currency_unread": 3}
    assert line["price_unreadable_top_hosts"] == {"b.example.com": 3}
    assert line["price_changed"] == 4 and "unrelated" not in line


# --------------------------------------------------------------------------------------------
# The offer projection's reader of stored variant prices
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "variant, expected",
    [
        ({"price": "28,80"}, 28.8),  # old: 2880
        ({"price": "1.234,56", "price_currency": "EUR"}, 1234.56),  # old: 1.23456
        ({"price": "1,234", "currency": "USD"}, 1234.0),
        ({"price": "1,234"}, None),  # no currency, no signal: refused, not guessed
        ({"price_amount": 19.99}, 19.99),
    ],
)
def test_variant_own_price_reads_comma_decimals(variant, expected) -> None:
    from services.catalog_enrichment_agent.ingestion import variant_own_price

    got = variant_own_price(variant)
    assert got == (pytest.approx(expected) if expected is not None else None)
