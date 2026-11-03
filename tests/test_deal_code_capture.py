"""Shadow capture of merchant-published codes from store pages the refresh already downloads
(services/deal_code_capture.py; ADR-027 D2 `merchant_published`): only promo regions are read, codes and
their stated terms are parsed, counters and one log line per page with codes are the only output, and the
page refresh never pays for it. No database, no network."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import deal_code_capture as dcc  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_stats():
    dcc.reset_stats()
    yield
    dcc.reset_stats()


def _codes(html):
    return {c.code: c for c in dcc.capture_codes(html)}


# ---- where it reads ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opening,closing",
    [
        ('<div class="announcement-bar">', "</div>"),
        ('<div id="shopify-section-announcement-bar">', "</div>"),
        ('<section class="promo-bar">', "</section>"),
        ('<div class="top_bar">', "</div>"),
        ('<div class="marquee">', "</div>"),
        ('<div class="header-message">', "</div>"),
        ('<announcement-bar>', "</announcement-bar>"),  # a theme's custom element
        ('<div aria-label="Announcement">', "</div>"),
    ],
)
def test_promo_regions_are_found_by_class_id_tag_or_label(opening, closing):
    html = f"<html><body>{opening}<p>Use code GLOW20 for 20% off</p>{closing}</body></html>"
    assert set(_codes(html)) == {"GLOW20"}


def test_product_copy_and_scripts_are_never_read():
    html = (
        '<div class="product__description">Use code SHADE1 never. Shade code: NC20</div>'
        '<div class="announcement-bar"><script>var m="use code HIDDEN1";</script>'
        '<style>.x{content:"use code HIDDEN2"}</style><span>No code here</span></div>'
    )
    assert _codes(html) == {}


def test_a_region_spans_its_nested_children_and_survives_unclosed_tags():
    html = '<div class="announcement-bar"><ul><li>Free shipping<li><b>enter code <i>SAVE15</i></b> today</ul></div><p>after</p>'
    assert set(_codes(html)) == {"SAVE15"}
    assert dcc.promo_regions(html) == ["Free shipping enter code SAVE15 today"]


def test_region_text_is_bounded():
    html = '<div class="announcement-bar">' + "x " * 10000 + "</div>"
    assert len(dcc.promo_regions(html)[0]) <= 4000


# ---- what it parses ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,code",
    [
        ("Use code GLOW20 at checkout", "GLOW20"),
        ("ENTER CODE: SPRING-15", "SPRING-15"),
        ("apply the discount code “FALL10”", "FALL10"),
        ("Promo code: VIP2026", "VIP2026"),
        ("coupon code：TAKE5", "TAKE5"),
    ],
)
def test_published_code_forms(text, code):
    assert set(_codes(f'<div class="announcement-bar">{text}</div>')) == {code}


@pytest.mark.parametrize(
    "text",
    [
        "Use code at checkout",  # no code token
        "use code glow20",  # stores print codes in capitals; lower case is prose
        "Use code 2026",  # digits only
        "Use code FREE shipping",  # a stop word
        "code: NC20",  # no promo/discount/coupon/voucher before a bare "code:"
    ],
)
def test_text_that_is_not_a_published_code(text):
    assert _codes(f'<div class="announcement-bar">{text}</div>') == {}


def test_terms_stated_near_the_code_are_metadata():
    html = (
        '<div class="announcement-bar">Use code GLOW20 for 20% off orders over $50. Excludes sale items.</div>'
        '<div class="promo-bar">New customers: enter code WELCOME10 - $10 off your first order</div>'
        '<div class="top-bar">Sign up for our newsletter and use code JOIN5 for free shipping</div>'
        '<div class="ticker">Use code ONCE1, one use per customer, spend £30 for 15% off</div>'
    )
    codes = _codes(html)
    glow, welcome, join, once = codes["GLOW20"], codes["WELCOME10"], codes["JOIN5"], codes["ONCE1"]
    assert (glow.kind, glow.value, glow.min_subtotal, glow.hints) == ("percent", "20", "50", ["has_exclusions"])
    assert (welcome.kind, welcome.value, welcome.currency_symbol, welcome.hints) == ("amount", "10", "$", ["new_customer_only"])
    assert (join.kind, join.hints) == ("free_shipping", ["segment_limited"])
    assert (once.kind, once.min_subtotal, once.currency_symbol, once.hints) == ("percent", "30", "£", ["once_per_customer"])


def test_a_code_with_no_stated_terms_has_none():
    c = _codes('<div class="announcement-bar">Use code MYSTERY9</div>')["MYSTERY9"]
    assert (c.kind, c.value, c.min_subtotal, c.hints) == (None, None, None, [])


def test_one_code_is_reported_once_per_page_whatever_its_case_or_repeats():
    html = '<div class="announcement-bar">Use code GLOW20</div><div class="marquee">Use code GLOW20 Use code GLOW20</div>'
    assert len(dcc.capture_codes(html)) == 1


# ---- shadow: counters and the log line -----------------------------------------------------------------------


def test_shadow_counts_pages_regions_codes_and_distinct_host_codes(capsys):
    with_code = '<div class="announcement-bar">Use code GLOW20 for 20% off</div>'
    dcc.capture_shadow(with_code, url="https://www.glow.example/p/1", market="US", page_currency="USD")
    dcc.capture_shadow(with_code, url="https://glow.example/p/2", market="US", page_currency="USD")  # same host, same code
    dcc.capture_shadow('<div class="announcement-bar">Free shipping</div>', url="https://b.example/p", market="SG",
                       page_currency="SGD")
    dcc.capture_shadow("<p>nothing</p>", url="https://c.example/p", market="US", page_currency=None)
    s = dcc.stats()
    assert (s["pages_scanned"], s["pages_with_region"], s["pages_with_code"]) == (4, 3, 2)
    assert (s["codes_seen"], s["distinct_host_codes"], s["hosts_with_codes"]) == (1, 1, 1)
    assert (s["by_kind"], s["by_market"], s["by_hint"]) == ({"percent": 1}, {"US": 2}, {"none": 1})
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("DEAL_CODE_CAPTURE_PAGE ")]
    assert len(lines) == 2  # one per page WITH codes; none for the pages without
    payload = json.loads(lines[0].split(" ", 1)[1])
    assert (payload["host"], payload["market"], payload["page_currency"], payload["codes"][0]["code"]) == (
        "glow.example", "US", "USD", "GLOW20",
    )


def test_shadow_never_raises_and_counts_the_error(monkeypatch):
    def boom(html):
        raise RuntimeError("parser broke")

    monkeypatch.setattr(dcc, "promo_regions", boom)
    dcc.capture_shadow("<div>", url="https://a.example", market="US", page_currency=None)
    assert dcc.stats()["errors"] == 1


def test_shadow_survives_html_that_is_not_text():
    dcc.capture_shadow(None, url="https://a.example", market="US", page_currency=None)
    assert dcc.stats()["errors"] == 0 and dcc.stats()["pages_scanned"] == 1


# ---- the hook and the flag -----------------------------------------------------------------------------------


def test_the_flag_defaults_off(monkeypatch):
    from config.settings import settings

    monkeypatch.delenv("DEAL_CODE_CAPTURE_SHADOW", raising=False)
    assert settings.deal_code_capture_shadow is False
    monkeypatch.setenv("DEAL_CODE_CAPTURE_SHADOW", "true")
    assert settings.deal_code_capture_shadow is True


@pytest.mark.parametrize("flag,called", [("true", True), ("false", False), (None, False)])
def test_the_page_refresh_captures_only_with_the_flag(monkeypatch, flag, called):
    import services.external_offers_service as svc

    if flag is None:
        monkeypatch.delenv("DEAL_CODE_CAPTURE_SHADOW", raising=False)
    else:
        monkeypatch.setenv("DEAL_CODE_CAPTURE_SHADOW", flag)
    page = '<html><head><title>Toner</title></head><body><div class="announcement-bar">Use code GLOW20</div></body></html>'
    seen = []

    async def fake_fetch_html(url, **kwargs):
        return page, "text/html"

    async def no_existing(*a, **k):
        return None

    def spy(html, **kwargs):
        seen.append((html, kwargs))
        raise RuntimeError("stop after the hook")  # capture_shadow never raises; the spy stops the refresh here

    monkeypatch.setattr(svc, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(svc, "_get_snapshot_row", no_existing)
    monkeypatch.setattr(svc.deal_code_capture, "capture_shadow", spy)

    async def _boom(*a, **k):
        raise RuntimeError("no database here")

    monkeypatch.setattr(svc.database, "execute", _boom)
    monkeypatch.setattr(svc.database, "fetch_one", _boom)
    try:
        asyncio.run(svc.resolve_external_offer(market="us", url="https://brand.example/products/toner", force_refresh=True))
    except Exception:
        pass
    if called:
        assert len(seen) == 1
        html, kwargs = seen[0]
        assert html == page
        assert (kwargs["url"], kwargs["market"]) == ("https://brand.example/products/toner", "US")
    else:
        assert seen == []


@pytest.mark.parametrize("flag", [True, False])
def test_the_refresh_job_reports_the_shadow_counts_only_with_the_flag(monkeypatch, flag):
    import jobs.external_referral_refresh as job

    if flag:
        monkeypatch.setenv("DEAL_CODE_CAPTURE_SHADOW", "true")
    else:
        monkeypatch.delenv("DEAL_CODE_CAPTURE_SHADOW", raising=False)

    async def fake_batch(**kwargs):
        dcc.capture_shadow('<div class="announcement-bar">Use code GLOW20</div>', url="https://a.example",
                           market="US", page_currency="USD")
        return {"status": "success"}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    summary = asyncio.run(job.run_daily_external_referral_refresh(limit=1, budget_seconds=10.0))
    if flag:
        assert summary["deal_code_capture"]["codes_seen"] == 1
    else:
        assert "deal_code_capture" not in summary  # the summary is unchanged while the flag is off
