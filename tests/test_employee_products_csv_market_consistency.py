"""Tests for the market-consistency validators that gate CSV imports
into external_product_seeds.

Background: prod has 469 EUR/US seeds + 12 KR-domain US-market seeds
that entered through routes/employee_products.py CSV imports without
any (market, currency) or (market, domain) sanity check. The mismatch
surfaced as wrong-currency prices and Korean-language PDPs in US-user
shopping-agent recall. These validators reject the bad rows at import
so the data never reaches external_product_seeds.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.employee_products import (  # noqa: E402
    MARKET_EXPECTED_CURRENCY,
    validate_market_currency,
    validate_market_domain,
)


# ---------------------------------------------------------------------------
# validate_market_currency
# ---------------------------------------------------------------------------


def test_market_currency_accepts_matching_pair() -> None:
    assert validate_market_currency("US", "USD") is None
    assert validate_market_currency("KR", "KRW") is None
    assert validate_market_currency("JP", "JPY") is None
    assert validate_market_currency("DE", "EUR") is None


def test_market_currency_rejects_us_with_eur() -> None:
    """The trigger: 469 KraveBeauty + others were imported with
    market=US currency=EUR via CSV. Validator must reject them."""
    err = validate_market_currency("US", "EUR")
    assert err is not None
    assert "market_currency_mismatch" in err
    assert "market_US" in err
    assert "currency_EUR" in err


def test_market_currency_rejects_kr_with_usd() -> None:
    """Symmetric: KR seller in KRW; mislabelled with USD must reject."""
    err = validate_market_currency("KR", "USD")
    assert err is not None
    assert "market_KR" in err
    assert "currency_USD" in err


def test_market_currency_permissive_when_currency_missing() -> None:
    """Empty/None currency is allowed at validate time — downstream
    fills it from the market default. Rejecting here would block
    legitimate CSVs that omit currency entirely."""
    assert validate_market_currency("US", None) is None
    assert validate_market_currency("US", "") is None
    assert validate_market_currency("US", "  ") is None


def test_market_currency_permissive_for_unknown_market() -> None:
    """If we don't have an expected currency for the market in
    MARKET_EXPECTED_CURRENCY (e.g., a new market not yet added),
    don't block — defer to caller's judgment."""
    assert validate_market_currency("ZZ", "EUR") is None
    assert validate_market_currency("XX", "USD") is None


def test_market_currency_handles_lowercase_input() -> None:
    """CSVs may have lowercase 'usd' / 'eur'. Validator normalizes."""
    assert validate_market_currency("US", "usd") is None
    assert validate_market_currency("us", "USD") is None
    err = validate_market_currency("us", "eur")
    assert err is not None


def test_market_expected_currency_map_covers_top_markets() -> None:
    """Pin the major markets so a future refactor doesn't accidentally
    drop one and silently make all imports for that market permissive."""
    for market in ("US", "GB", "DE", "JP", "KR", "CN", "SG", "AU"):
        assert market in MARKET_EXPECTED_CURRENCY, f"missing {market}"


# ---------------------------------------------------------------------------
# validate_market_domain
# ---------------------------------------------------------------------------


def test_market_domain_rejects_us_with_kr_tld() -> None:
    """The trigger: 12 .co.kr / .kr seeds imported as market=US.
    Result was Korean-language PDPs in US recall."""
    err = validate_market_domain("US", "https://roundlab.co.kr/product/x")
    assert err is not None
    assert "market_domain_mismatch" in err
    assert "host_roundlab.co.kr" in err

    err2 = validate_market_domain("US", "https://example.kr/p/y")
    assert err2 is not None
    assert "host_example.kr" in err2


def test_market_domain_rejects_us_with_jp_tld() -> None:
    err = validate_market_domain("US", "https://example.co.jp/product/x")
    assert err is not None
    assert "host_example.co.jp" in err


def test_market_domain_accepts_us_market_us_domain() -> None:
    assert validate_market_domain("US", "https://kravebeauty.com/products/x") is None
    assert validate_market_domain("US", "https://www.tomfordbeauty.com/products/y") is None
    assert validate_market_domain("US", "https://shop.example.org/p") is None


def test_market_domain_skips_validation_for_non_us_market() -> None:
    """v1: only validate US-market mismatches. Non-US markets often
    legitimately import from .com domains (Sephora.fr buyer journey
    can pass through sephora.com), so a strict TLD check would
    over-reject. Conservative scope kept for now."""
    assert validate_market_domain("FR", "https://sephora.com/product") is None
    assert validate_market_domain("KR", "https://kravebeauty.com/p") is None


def test_market_domain_permissive_on_invalid_url() -> None:
    """A malformed URL just falls through; the existing URL validator
    elsewhere (`_require_http_url`) catches that. This validator
    should never crash on bad input."""
    assert validate_market_domain("US", "") is None
    assert validate_market_domain("US", None) is None
    assert validate_market_domain("US", "not-a-url") is None
