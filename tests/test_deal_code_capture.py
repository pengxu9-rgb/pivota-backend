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
        ('<div class="announcementBar">', "</div>"),
        ('<div class="ticker-bar">', "</div>"),
        ('<marquee>', "</marquee>"),
        ('<div class="shopify-section announcement-bar color-scheme-1">', "</div>"),  # one token of several
        ('<div id="shopify-section-sections--1656__announcement-bar">', "</div>"),  # a section id, not a modifier
        ('<div class="announcement-bar announcement-bar-open">', "</div>"),  # a state token beside a name
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
    assert dcc.promo_regions(html) == ["Free shipping\nenter code SAVE15 today"]


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
        '<div class="ticker-bar">Use code ONCE1, one use per customer, spend £30 for 15% off</div>'
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
    import hashlib

    assert (payload["host"], payload["market"], payload["page_currency"]) == ("glow.example", "US", "USD")
    # The code itself is never printed (review of be#2732): a short hash of host+code, and its description.
    assert set(payload["codes"][0]) == {"kind", "shape", "form", "region", "hints"}  # no code, no hash
    assert "GLOW20" not in lines[0]


def test_shadow_never_raises_and_counts_the_error(monkeypatch):
    def boom(html):
        raise RuntimeError("parser broke")

    monkeypatch.setattr(dcc, "_scan", boom)
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


# ---- review of c0ecc10c5: region names are whole tokens ------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        '<div class="product-sticker">Use code STK10</div>',  # "ticker" inside a word
        '<div class="card"><span class="price-sticker">Use code STK20</span></div>',
        '<div id="stock-ticker">Use code TIC10</div>',  # bare ticker is not a promo name
        '<div class="desktop-bar">Use code DSK10</div>',  # "top-bar" inside a word
        '<div class="laptop_bar">Use code LAP10</div>',
        '<div class="sticky-top bar-nav">Use code XTK10</div>',  # across two tokens
        '<div class="website-banner">Use code WEB10</div>',  # "site-banner" inside a word
        '<nav aria-label="Top bar menu">Use code NAV10</nav>',  # a label is prose; only "Announcement" counts
        '<div class="banner">Use code BAN10</div>',  # a bare banner is a hero, not a promo bar
        '<div class="hero-banner">Use code HERO10</div>',
        '<div class="promo-barcode">Use code BCD10</div>',  # a name must END the token or precede -/_
        '<button aria-label="Close announcement">Use code BTN10</button>',  # the label must BE the name
        '<div class="page-wrapper has-announcement-bar">Use code WRAP10</div>',  # page-state tokens
        '<section class="with-top-bar">Use code WRAP30</section>',
        '<div class="no-top-bar">Use code WRAP50</div>',
        '<div class="page announcement-bar-visible"><div class="rte">Use code WRAP60</div></div>',
        '<div class="announcement-bar-active"><div class="rte">Use code WRAP61</div></div>',
        '<div class="layout marquee-enabled"><div class="rte">Use code WRAP62</div></div>',
        '<div class="site top-bar-open"><div class="rte">Use code WRAP63</div></div>',
    ],
)
def test_a_name_inside_another_word_or_a_bare_banner_is_not_a_region(html):
    assert _codes(html) == {}


@pytest.mark.parametrize("root", ["body", "main", "header", "html"])
def test_a_page_root_whose_class_names_a_bar_is_not_one_region(root):
    html = (
        f'<{root} class="template-product has-announcement-bar">'
        '<div class="announcement-bar">Use code BAR10 for 10% off</div>'
        '<div class="product__description">Use code DESC10 for 10% off</div>'
        f"</{root}>"
    )
    assert set(_codes(html)) == {"BAR10"}


@pytest.mark.parametrize("root", ["body", "main", "header", "html"])
@pytest.mark.parametrize("cls", ["announcement-bar", "announcement-bar--visible", "announcement-bar-container"])
def test_a_page_root_is_never_a_region_even_when_its_class_is_the_bar_name(root, cls):
    # A name, modifier or wrapper token, not a has-/with- state class: only the root rule keeps the product
    # copy out, for a fallback region as for a named one.
    html = f'<{root} class="{cls}"><div class="product__description">Use code DESC10</div></{root}>'
    assert _codes(html) == {}


@pytest.mark.parametrize("tag", ["script", "style", "noscript", "template", "svg"])
def test_every_skipped_tag_is_skipped_inside_a_region(tag):
    html = f'<div class="announcement-bar"><{tag}>Use code HID10</{tag}><span>Use code SEEN10</span></div>'
    assert set(_codes(html)) == {"SEEN10"}


@pytest.mark.parametrize("tag", ["script", "style", "noscript", "template", "svg"])
def test_a_region_after_a_skipped_tag_is_still_read(tag):
    # The skip ends with its tag: a page whose first <script> silently hid every later bar reads zero.
    html = f'<{tag}>var a = 1;</{tag}><div class="announcement-bar">Use code AFTER10</div>'
    assert set(_codes(html)) == {"AFTER10"}


def test_hidden_text_is_read():
    # Markup, not rendering: a rotating bar keeps its other slides hidden.
    html = '<div class="announcement-bar"><p style="display:none" aria-hidden="true">Use code HIDE10</p></div>'
    assert set(_codes(html)) == {"HIDE10"}


# ---- review of c0ecc10c5: bounded parse ------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["<p><b>x</b>", "<p>x</div>", "<li>x", "<p>x</span>"])
def test_the_parse_is_linear_on_a_full_size_page(unit):
    import time

    html = '<body><div class="announcement-bar">Use code TOP10</div>' + unit * (1_200_000 // len(unit))
    started = time.perf_counter()
    assert set(_codes(html)) == {"TOP10"}
    assert time.perf_counter() - started < 3.0  # was 47-54 s at c0ecc10c5


def test_only_the_top_of_the_body_is_read_and_the_head_does_not_count():
    head = "<head><script>" + "x" * 300_000 + "</script></head>"
    top = '<div class="announcement-bar">Use code TOP10</div>'
    deep = '<div class="announcement-bar">Use code DEEP10</div>'
    html = f"<html>{head}<body>{top}" + "<p>filler</p>" * (dcc._BODY_BUDGET // 13 + 4_000) + f"{deep}</body></html>"
    assert set(_codes(html)) == {"TOP10"}
    dcc.capture_shadow(html, url="https://a.example", market="US", page_currency=None)
    assert dcc.stats()["pages_over_budget"] == 1


def test_nesting_past_the_depth_cap_stops_the_parse_without_failing():
    html = '<div class="announcement-bar">Use code TOP10</div>' + "<div>" * 5000 + '<div class="announcement-bar">Use code LOW10</div>'
    assert set(_codes(html)) == {"TOP10"}
    dcc.capture_shadow(html, url="https://a.example", market="US", page_currency=None)
    s = dcc.stats()
    assert (s["pages_too_deep"], s["errors"]) == (1, 0)


def test_unclosed_siblings_close_each_other():
    html = "<ul>" + "<li>item" * 2000 + '</ul><div class="announcement-bar">Use code LIST10</div>'
    assert set(_codes(html)) == {"LIST10"}


def test_a_page_reports_at_most_twenty_codes_and_its_log_line_stays_small(capsys):
    html = "".join(f'<div class="announcement-bar">Use code C{i:05d}X for 10% off orders over $50</div>' for i in range(20000))
    assert len(dcc.capture_codes(html)) == 20
    dcc.capture_shadow(html, url="https://a.example", market="US", page_currency="USD")
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("DEAL_CODE_CAPTURE_PAGE ")][0]
    assert len(line.encode()) < 32_000  # Cloud Logging drops entries over 256 KB; c0ecc10c5 printed 3.18 MB
    assert json.loads(line.split(" ", 1)[1])["codes_capped"] is True
    assert (dcc.stats()["pages_codes_capped"], dcc.stats()["codes_seen"]) == (1, 20)


def test_the_snippet_is_bounded():
    code = "LONGCODE123456789012"  # the longest code: 60 before + 20 + 90 after is 170 before the bound
    c = _codes('<div class="announcement-bar">' + "word " * 500 + f"Use code {code} " + "word " * 500 + "</div>")[code]
    assert code in c.snippet and len(c.snippet) == 160


def test_the_min_spend_pattern_does_not_backtrack_on_blank_runs():
    import time

    started = time.perf_counter()
    dcc._MIN_SPEND_RE.search("orders " + " " * 100_000)
    assert time.perf_counter() - started < 1.0  # was 107 s


# ---- review of c0ecc10c5: what is a code ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "USE CODE BELOW FOR 10% OFF",
        "USE CODE FOR FREE SHIPPING",
        "USE CODE ONLINE OR IN STORE",
        "ENTER CODE WHEN YOU CHECK OUT",
        "APPLY CODE DURING CHECKOUT",
        "PROMO CODE: NONE NEEDED",
        "USE CODE TODAY ONLY",
        "Apply code NC20 shade swatch",  # a shade name, not a code
        "Use code NW15 tone",
        "Enter code 1234-5678-ABCD",  # a gift-card number: dash groups of four
        "Enter code XXXX at checkout",  # a placeholder
        "USE CODE NEXT TIME",
        "USE CODE SHOWN",
        "USE CODE IN-STORE",
        "ENTER CODE LATER",
        "APPLY CODE MANUALLY",
        "USE CODE FIRST ORDER",
        "USE CODE EVERY TIME",
        "USE CODE ATCHECKOUT",
        "USE CODE INSTEAD",
        "USE CODE ALWAYS",
        "USE CODE SOON",
        "USE CODE YOU LOVE",
        "USE CODE: NEW",
        "PROMO CODE: TBA",
        "USE CODE MON-FRI",
        "USE CODE SEE DETAILS",
        "USE CODE SENT TO YOUR INBOX",
        "USE CODE FIELD AT CHECKOUT",
        "USE CODE MEMBERS ONLY",
        "USE CODE ASAP",
    ],
)
def test_prose_shades_gift_cards_and_placeholders_are_not_codes(text):
    assert _codes(f'<div class="announcement-bar">{text}</div>') == {}


def test_shape_and_form_are_reported_so_shade_like_codes_stay_visible():
    html = (
        '<div class="announcement-bar">Use code BF25 today. Promo code: WELCOME. Use code GLOW20. '
        "Use code VIP.</div>"
    )
    codes = _codes(html)
    assert {k: (c.shape, c.form) for k, c in codes.items()} == {
        "BF25": ("short_alnum", "use_code"), "WELCOME": ("letters", "labelled"), "GLOW20": ("alnum", "use_code"),
        "VIP": ("short_letters", "use_code"),
    }
    dcc.capture_shadow(html, url="https://a.example", market="US", page_currency=None)
    s = dcc.stats()
    assert (s["by_shape"], s["by_form"]) == (
        {"short_alnum": 1, "letters": 1, "alnum": 1, "short_letters": 1}, {"use_code": 3, "labelled": 1}
    )


def test_a_code_split_by_inline_tags_is_joined_and_block_tags_still_separate_words():
    assert set(_codes('<div class="announcement-bar">Use code <strong>SAVE</strong>10</div>')) == {"SAVE10"}
    assert dcc.promo_regions('<div class="announcement-bar"><p>Free</p><p>shipping</p></div>') == ["Free\nshipping"]


def test_one_hundred_percent_off_is_read_whole():
    c = _codes('<div class="announcement-bar">Use code FREE100 for 100% off</div>')["FREE100"]
    assert (c.kind, c.value) == ("percent", "100")


def test_each_stated_term_goes_to_the_nearest_code():
    html = '<div class="announcement-bar">Use code AAA10 for 10% off | Use code BBB20 for free shipping | Promo code: CCC30 $5 off</div>'
    codes = _codes(html)
    assert {k: (c.kind, c.value) for k, c in codes.items()} == {
        "AAA10": ("percent", "10"), "BBB20": ("free_shipping", None), "CCC30": ("amount", "5"),
    }


# ---- review of c0ecc10c5: counters ------------------------------------------------------------------------------


def test_reset_clears_the_host_codes_so_a_new_run_counts_them_again():
    page = '<div class="announcement-bar">Use code GLOW20</div>'
    dcc.capture_shadow(page, url="https://a.example", market="US", page_currency=None)
    dcc.reset_stats()
    dcc.capture_shadow(page, url="https://a.example", market="US", page_currency=None)
    s = dcc.stats()
    assert (s["codes_seen"], s["distinct_host_codes"], s["hosts_with_codes"]) == (1, 1, 1)


def test_a_failed_print_moves_no_counter(monkeypatch):
    def broken_print(*a, **k):
        raise OSError("stdout closed")

    monkeypatch.setattr(dcc, "print", broken_print, raising=False)
    dcc.capture_shadow('<div class="announcement-bar">Use code GLOW20</div>', url="https://a.example", market="US",
                       page_currency=None)
    s = dcc.stats()
    assert (s["errors"], s["pages_scanned"], s["codes_seen"]) == (1, 0, 0)


def test_a_lone_surrogate_does_not_fail_the_log_line(capsys):
    dcc.capture_shadow('<div class="announcement-bar">\ud800 Use code SUR10</div>', url="https://a.example",
                       market="US", page_currency=None)
    assert dcc.stats()["errors"] == 0
    out = capsys.readouterr().out
    assert "DEAL_CODE_CAPTURE_PAGE" in out
    assert "SUR10" not in out


def test_bytes_are_not_parsed():
    assert dcc.capture_codes(b'<div class="announcement-bar">Use code BYT10</div>') == []


def test_the_refresh_job_resets_the_counters_when_it_starts(monkeypatch):
    import jobs.external_referral_refresh as job

    monkeypatch.setenv("DEAL_CODE_CAPTURE_SHADOW", "true")
    dcc.capture_shadow('<div class="announcement-bar">Use code OLD10</div>', url="https://a.example", market="US",
                       page_currency=None)

    async def fake_batch(**kwargs):
        return {"status": "success"}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    summary = asyncio.run(job.run_daily_external_referral_refresh(limit=1, budget_seconds=10.0))
    assert (summary["deal_code_capture"]["pages_scanned"], summary["deal_code_capture"]["codes_seen"]) == (0, 0)


@pytest.mark.parametrize("flag", [True, False])
def test_the_job_prints_its_capture_line_only_with_the_flag(monkeypatch, capsys, flag):
    import jobs.external_referral_refresh as job
    from db.database import database as db_singleton

    if flag:
        monkeypatch.setenv("DEAL_CODE_CAPTURE_SHADOW", "true")
    else:
        monkeypatch.delenv("DEAL_CODE_CAPTURE_SHADOW", raising=False)

    async def nothing():
        return None

    async def fake_batch(**kwargs):
        return {"status": "success", "candidate_count": 0, "refreshed": 0}

    monkeypatch.setattr(db_singleton, "connect", nothing)
    monkeypatch.setattr(db_singleton, "disconnect", nothing)
    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    monkeypatch.setattr(sys, "argv", ["external_referral_refresh", "--limit", "1"])
    job.main()
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("DEAL_CODE_CAPTURE ")]
    assert len(lines) == (1 if flag else 0)


# ---- re-review of b6debd7f4 --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,code",
    [
        ("Use code SAVE20 for 20% off. Excludes gift cards.", "SAVE20"),
        ("Use code SAVE20 - not valid on gift cards", "SAVE20"),
        ("Use code HOLIDAY15 on everything (gift cards excluded)", "HOLIDAY15"),
    ],
)
def test_a_gift_card_disclaimer_keeps_the_code_and_becomes_a_hint(text, code):
    c = _codes(f'<div class="announcement-bar">{text}</div>')[code]
    assert c.hints == ["has_exclusions", "mentions_gift_card"]


@pytest.mark.parametrize(
    "code",
    [
        "VIP", "JOY", "GET-10-OFF", "BLACK-FRIDAY-20", "SUMMER-20", "10OFF", "TAKE5",
        "SAVE-2024",  # two groups of four is a code; only three or more is a gift-card number
        "SAVE5-1234-5678",  # groups of four inside a longer token are not a gift-card number
    ],
)
def test_short_and_dashed_codes_are_codes(code):
    assert set(_codes(f'<div class="announcement-bar">Use code {code} at checkout</div>')) == {code}


def test_a_wrapper_state_class_does_not_hide_the_bar_inside_it():
    html = (
        '<div class="page-wrapper has-announcement-bar"><div class="announcement-bar">Use code BAR10</div>'
        '<div class="product__description">Use code DESC10 for 10% off</div></div>'
    )
    assert set(_codes(html)) == {"BAR10"}


def test_a_stray_end_tag_inside_a_bar_does_not_close_the_bar():
    # The second </b> has nothing open to close; it must not pop the bar itself.
    assert set(_codes('<div class="announcement-bar"><b>Hi</b></b> Use code SAVE10</div>')) == {"SAVE10"}


@pytest.mark.parametrize(
    "text,kind,value",
    [
        ("Use code ZERO1 for 0% off", None, None),
        ("Use code BIG1 for 1100% off", None, None),  # never "100" out of 1100
        ("Use code SAVE10 for 0% off then 20% off", "percent", "20"),  # the first VALID percent wins
    ],
)
def test_only_a_percent_from_1_to_100_is_a_term(text, kind, value):
    c = next(iter(_codes(f'<div class="announcement-bar">{text}</div>').values()))
    assert (c.kind, c.value) == (kind, value)


def test_a_body_tag_inside_a_head_script_is_not_the_body():
    html = (
        '<html><head><script>document.write("<body>")</script><script>' + "x" * 250_000 + "</script></head>"
        '<body><div class="announcement-bar">Use code TOP10</div></body></html>'
    )
    assert set(_codes(html)) == {"TOP10"}


def test_a_custom_element_starting_with_body_is_not_the_body():
    html = '<div class="announcement-bar">Use code TOP10</div><body-scroll-lock></body-scroll-lock>'
    assert set(_codes(html)) == {"TOP10"}


# Sized so the bar after it still falls inside the body budget.
@pytest.mark.parametrize("token", ["-" * 190_000 + "_", "x--" * 63_000 + "_"], ids=["dashes", "x-dash-dash"])
def test_a_long_class_token_is_checked_in_linear_time(token):
    import time

    html = f'<html><head></head><body><div class="{token}">x</div><div class="announcement-bar">Use code BAR10</div>'
    started = time.perf_counter()
    assert set(_codes(html)) == {"BAR10"}
    assert time.perf_counter() - started < 1.0  # was 52-102 s at abb21bbe8


@pytest.mark.parametrize(
    "token,kind",
    [
        ("announcement-bar--sticky", "wrapper"),
        ("announcement-bar--visible", "wrapper"),  # a modifier on the bar itself, not a page state
        ("x--a--b", "wrapper"),
        ("shopify-section-sections--1__announcement-bar", "name"),
        ("a--b_c--d", "wrapper"),  # the LAST modifier has no underscore after it
        ("a--b_c", "name"),
        ("has-announcement-bar", "state"),
        ("no-top-bar", "state"),
        ("hide-announcement-bar--x", "state"),  # a leading state wins over a modifier
        ("announcement-bar-visible", "state"),
        ("top-bar-open", "state"),
        ("announcement-bar-container", "wrapper"),
        ("announcement-area", "wrapper"),
        ("announcement-bar", "name"),
        ("casino-announcement-bar", "name"),  # a state word only counts at the start
        ("announcement-bar-container__inner", "name"),  # a wrapper word only counts at the end
        ("announcement-bar-visible__text", "name"),  # a state word only counts at the end
    ],
)
def test_a_token_is_a_page_state_a_wrapper_or_a_name(token, kind):
    assert dcc._token_kind(token) == kind


@pytest.mark.parametrize(
    "code,shape",
    [("VIP", "short_letters"), ("BOGO", "letters"), ("NC20", "short_alnum"), ("GLOW20", "alnum")],
)
def test_three_letter_codes_have_their_own_shape(code, shape):
    assert dcc._shape(code) == shape


def test_a_head_past_the_budget_with_no_body_tag_reads_from_the_end_of_the_head():
    html = (
        "<html><head><style>" + "a" * 210_000 + "</style></head>"
        '<div class="announcement-bar">Use code TOP10</div></html>'
    )
    assert set(_codes(html)) == {"TOP10"}
    assert dcc._scan(html).over_budget is False


@pytest.mark.parametrize(
    "cls",
    ["announcement-bar--sticky", "announcement-wrapper", "announcement-bar-container", "promo-bar-wrap",
     "top-bar-holder", "announcement-area"],
)
def test_a_bar_named_only_by_a_wrapper_or_modifier_is_a_fallback_region(cls):
    html = f'<body><div class="{cls}">Use code BAR10 for 10% off</div></body>'
    assert {k: c.region for k, c in _codes(html).items()} == {"BAR10": "fallback"}


def test_a_wrapper_with_a_named_bar_inside_reads_only_the_bar():
    html = (
        '<body><div class="announcement-bar-container"><div class="announcement-bar__message">Use code BAR10</div>'
        '<div class="rte">Use code WRAP10</div></div></body>'
    )
    assert {k: c.region for k, c in _codes(html).items()} == {"BAR10": "named"}


def test_a_modifier_only_slide_inside_a_wrapper_is_read_once_as_a_fallback():
    html = (
        '<body><div class="announcement-bar-wrapper"><div class="announcement-bar__item--active">'
        "Use code SLIDE10</div></div></body>"
    )
    assert dcc.promo_regions(html) == ["Use code SLIDE10"]
    assert {k: c.region for k, c in _codes(html).items()} == {"SLIDE10": "fallback"}


def test_a_named_bar_wins_a_code_also_seen_in_a_fallback_and_both_are_counted_by_region(capsys):
    html = (
        '<body><div class="announcement-bar">Use code BAR10</div>'
        '<div class="top-bar-holder">Use code BAR10. Use code HOLD10</div></body>'
    )
    assert {k: c.region for k, c in _codes(html).items()} == {"BAR10": "named", "HOLD10": "fallback"}
    dcc.capture_shadow(html, url="https://a.example", market="US", page_currency=None)
    s = dcc.stats()
    assert (s["pages_with_region"], s["by_region"]) == (1, {"named": 1, "fallback": 1})
    assert '"region":"fallback"' in capsys.readouterr().out


def test_a_fallback_region_reads_all_of_its_text_when_a_modifier_slide_sits_inside_it():
    html = (
        '<body><div class="announcement-bar-container"><div class="announcement-bar--a">Use code ONE10</div>'
        '<div class="rte">Use code TWO10</div></div></body>'
    )
    assert dcc.promo_regions(html) == ["Use code ONE10\nUse code TWO10"]


def test_block_tags_separate_words_in_a_fallback_region():
    assert set(_codes('<body><div class="promo-bar-wrap"><p>Use code SAVE</p><p>10 more</p></div></body>')) == {"SAVE"}


def test_text_after_a_fallback_region_closes_is_not_read():
    html = '<body><div class="promo-bar-wrap">Free gift</div><div class="rte">Use code DESC10</div></body>'
    assert (dcc.promo_regions(html), _codes(html)) == (["Free gift"], {})


def test_a_page_with_only_a_fallback_region_counts_as_a_page_with_a_region():
    dcc.capture_shadow('<body><div class="promo-bar-wrap">Free gift</div></body>', url="https://a.example",
                       market="US", page_currency=None)
    assert dcc.stats()["pages_with_region"] == 1


@pytest.mark.parametrize(
    "html",
    [
        # no </head>: a <body> inside a head script is not the body; the real one 210 KB later is
        '<html><head><script>var t="<body>";</script><style>' + "a" * 210_000 + "</style><body>{bar}",
        # omitted </head>: a "</head>" in a body script after the bar does not move the start past it
        '<html><head><title>x</title><body>{bar}<script>var s="</head>";</script>',
        # an iframe srcdoc holding a whole document after the bar
        '<html><head><title>x</title><body>{bar}<iframe srcdoc="<html><head></head><body>hi</body></html>"></iframe>',
        # a <body> inside the head (malformed) still starts the body
        "<html><head><body>{bar}</head>",
        # no <body> tag: the first tag a head cannot hold starts it
        "<html><head></head>{bar}",
        "<html><head><title>x</title>{bar}",
        # a tracking pixel's <img> inside a head noscript does not start the body
        '<html><head><noscript><img src="p.gif"></noscript><style>' + "a" * 210_000 + "</style></head><body>{bar}",
    ],
)
def test_the_body_starts_at_the_first_body_tag_or_body_content_outside_head_raw_text(html):
    html = html.format(bar='<div class="announcement-bar">Use code BAR10</div>')
    assert set(_codes(html)) == {"BAR10"}


def test_a_head_past_the_scan_limit_starts_the_body_at_the_limit(monkeypatch):
    monkeypatch.setattr(dcc, "_HEAD_SCAN_LIMIT", 100)
    html = "<html><head><style>" + "a" * 500 + '</style></head><body><div class="announcement-bar">Use code BAR10</div>'
    assert dcc._body_start(html) == 100
    assert set(_codes(html)) == {"BAR10"}


def test_a_short_page_with_only_head_tags_starts_at_the_page_start():
    assert dcc._body_start("<html><head><title>x</title></head>") == 0


@pytest.mark.parametrize(
    "stray",
    [
        '<img src="p.gif" height="1" width="1">',  # a tracking pixel outside noscript
        "<shopify-analytics></shopify-analytics>",  # a custom element
        "<noscript>",  # an unclosed noscript
        '<div id="tracking"></div>',
    ],
)
def test_a_stray_tag_in_a_large_head_does_not_end_the_head(stray):
    html = (
        "<html><head>" + stray + "<script>" + "x" * 250_000 + "</script></head>"
        '<body><div class="announcement-bar">Use code BAR10</div></body></html>'
    )
    assert html[dcc._body_start(html):].startswith("<body>")
    assert set(_codes(html)) == {"BAR10"}


def test_with_no_body_tag_the_body_starts_at_the_first_tag_a_head_cannot_hold():
    html = (
        "<html><head><title>x</title><meta charset=utf-8><link rel=x href=y><base href=z><style>a{}</style>"
        "<script>var a;</script><template><p>t</p></template><noscript><img src=p.gif></noscript></head>"
        '<img src="logo.png"><div class="announcement-bar">Use code BAR10</div>'
    )
    assert dcc._body_start(html) == html.index('<img src="logo.png">')


def test_the_body_start_offset_counts_lines():
    html = "<html>\n<head>\n<title>x</title>\n</head>\n<body class='a'>\n"
    assert html[dcc._body_start(html):].startswith("<body")


@pytest.mark.parametrize("unit", ["<", "&", "< ", "<meta name=x>", "<noscript>"])
def test_finding_the_body_is_linear_up_to_the_head_scan_limit(unit):
    import time

    html = "<html><head>" + unit * (dcc._HEAD_SCAN_LIMIT // len(unit))
    started = time.perf_counter()
    dcc._body_start(html)
    assert time.perf_counter() - started < 5.0  # about 0.5 s on a laptop for 1 MB of "<"


def test_entities_in_a_head_cost_nothing_to_scan():
    import time

    html = "<html><head>" + "&" * dcc._HEAD_SCAN_LIMIT
    started = time.perf_counter()
    dcc._body_start(html)
    assert time.perf_counter() - started < 0.2  # ~0.1 ms; was ~0.5 s per MB with convert_charrefs=False


# ---- diagnostics: counts only ------------------------------------------------------------------------------


def _diag_stats(html):
    dcc.capture_shadow(html, url="https://a.example/p", market="US", page_currency=None)
    return dcc.stats()


@pytest.mark.parametrize(
    "text,reason",
    [
        ("Use code welcome10 for 10% off", "lower_with_digit"),
        ("Use code welcome for 10% off", "lower_letters"),
        # Since the widened forms (2026-11-07), "with code SAVE10" and "Code: GLOW15" are captures. What is left as
        # "wording" is a shade-like short token after a bare "code", which the loosest form refuses.
        ("Code: NC20", "wording"),
        ("USE CODE BELOW", "stop"),
        ("Use code 2024 at checkout", "no_letter"),
        ("Use code NC20 shade", "rejected"),  # a shade name
        ("Use code XXXX", "rejected"),  # a placeholder
    ],
)
def test_each_relaxed_token_the_strict_matcher_missed_is_counted_by_reason(text, reason):
    s = _diag_stats(f'<body><div class="announcement-bar">{text}</div></body>')
    assert (s["pages_relaxed_code_match"], s["by_miss"], s["codes_seen"]) == (1, {reason: 1}, 0)


def test_a_captured_code_is_not_a_miss_and_a_token_is_counted_once_per_page():
    s = _diag_stats(
        '<body><div class="announcement-bar">Use code SAVE10. Use code save20. Code: save20</div>'
        '<div class="top-bar-holder">with code save20</div></body>'
    )
    assert (s["codes_seen"], s["by_miss"]) == (1, {"lower_with_digit": 1})


def test_named_and_fallback_only_pages_are_counted_apart():
    _diag_stats('<body><div class="announcement-bar">Free shipping</div></body>')
    _diag_stats('<body><div class="promo-bar-wrap">Free shipping</div></body>')
    _diag_stats('<body><div class="rte">Free shipping</div></body>')
    s = dcc.stats()
    assert (s["pages_with_region"], s["pages_with_named_region"], s["pages_with_fallback_region_only"]) == (2, 1, 1)


def test_a_region_mentioning_code_without_a_token_counts_only_the_mention():
    s = _diag_stats('<body><div class="announcement-bar">Codes sent by email</div></body>')
    assert (s["pages_region_mentions_code"], s["pages_relaxed_code_match"], s["by_miss"]) == (1, 0, {})


def test_a_strict_phrase_outside_any_region_is_counted_from_the_html():
    html = (
        '<html><head><script type="application/json">{"text":"Use code SAVE10 for 10% off"}</script></head>'
        '<body><div class="announcement-bar">Free shipping</div><div class="rte">Promo code: X1</div></body>'
    )
    s = _diag_stats(html)
    assert (s["pages_code_phrase_in_html"], s["pages_region_mentions_code"], s["codes_seen"]) == (1, 0, 0)
    assert _diag_stats('<body><div class="rte">no phrase here</div></body>')["pages_code_phrase_in_html"] == 1


def test_a_region_name_past_the_body_window_is_counted_only_for_a_name_token():
    filler = "<p>filler</p>" * (dcc._BODY_BUDGET // 13 + 4_000)
    s = _diag_stats(f'<body>{filler}<div class="announcement-bar">Use code LATE10</div></body>')
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"], s["codes_seen"]) == (1, 1, 0)
    for cls in ("has-announcement-bar", "product-card"):  # a state token, or no region name at all
        dcc.reset_stats()
        assert _diag_stats(f'<body>{filler}<div class="{cls}">x</div></body>')["pages_named_region_past_budget"] == 0
    dcc.reset_stats()
    s = _diag_stats('<body><div class="announcement-bar">x</div></body>')  # inside the window
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"]) == (0, 0)


@pytest.mark.parametrize(
    "length,bucket", [(39, "lt40"), (40, "40_199"), (199, "40_199"), (200, "200_999"), (999, "200_999"), (1000, "1000_plus")]
)
def test_each_region_length_is_bucketed(length, bucket):
    s = _diag_stats(f'<body><div class="announcement-bar">{"a" * length}</div></body>')
    assert s["by_region_chars"] == {bucket: 1}


def test_diagnostics_never_put_text_in_the_log_or_the_counters(capsys):
    # No strict code, so no page line: the misses below are counted, never printed or kept.
    html = '<body><div class="announcement-bar">with code hidden99, Code: NC20, use code below</div></body>'
    s = _diag_stats(html)
    assert s["by_miss"] == {"lower_with_digit": 1, "wording": 1, "stop": 1}
    assert capsys.readouterr().out == ""
    blob = json.dumps(s).lower()
    assert "hidden99" not in blob and "nc20" not in blob and "below" not in blob


def test_a_failing_diagnosis_keeps_the_page_capture_and_counts_itself(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(dcc, "_diagnose", boom)
    s = _diag_stats('<body><div class="announcement-bar">Use code SAVE10</div></body>')
    assert (s["errors"], s["diagnosis_errors"], s["pages_scanned"], s["codes_seen"], s["pages_with_region"]) == (
        0, 1, 1, 1, 1)
    assert (s["by_miss"], s["pages_region_mentions_code"]) == ({}, 0)
    assert "DEAL_CODE_CAPTURE_PAGE" in capsys.readouterr().out


def test_reset_clears_the_diagnostic_counters():
    _diag_stats('<body><div class="announcement-bar">with code save10</div></body>')
    dcc.reset_stats()
    s = dcc.stats()
    assert (s["pages_relaxed_code_match"], s["by_miss"], s["by_region_chars"], s["pages_with_named_region"]) == (0, {}, {}, 0)


@pytest.mark.parametrize(
    "unit",
    [
        "use ", "use" + " " * 10, "code ", "code: ", "promo code ", 'class="', "class='announcement-bar ", 'id="a ',
        "<p>x</p>", "<a ", "<a class='announcement-bar' ", " class='a' id='b' ", "use\n" + " " * 39,
        "enter the" + " " * 41, "<a " + "x" * 2_100,
    ],
)
def test_the_diagnostics_are_linear_on_hostile_pages(unit):
    import io
    import time
    from contextlib import redirect_stdout

    html = "<body>" + unit * (2_400_000 // len(unit))
    started = time.perf_counter()
    with redirect_stdout(io.StringIO()):
        dcc.capture_shadow(html, url="https://a.example/p", market="US", page_currency=None)
    assert time.perf_counter() - started < 5.0
    assert dcc.stats()["errors"] == 0


@pytest.mark.parametrize("text", ["Barcodes on the box", "Unicode support", "Encoded"])
def test_code_only_counts_as_a_whole_word(text):
    assert _diag_stats(f'<body><div class="announcement-bar">{text}</div></body>')["pages_region_mentions_code"] == 0


def test_a_labelled_phrase_in_a_script_is_counted_from_the_html():
    html = '<html><head><script>var t="Discount code: SPRING";</script></head><body><div class="rte">x</div></body>'
    assert _diag_stats(html)["pages_code_phrase_in_html"] == 1


def test_a_fallback_only_page_is_diagnosed_like_a_named_one():
    s = _diag_stats('<body><div class="promo-bar-wrap">Shop with code save10</div></body>')
    assert (s["pages_with_fallback_region_only"], s["pages_region_mentions_code"], s["by_miss"], s["by_region_chars"]) == (
        1, 1, {"lower_with_digit": 1}, {"lt40": 1})


def test_a_page_with_named_and_fallback_regions_is_not_fallback_only():
    s = _diag_stats('<body><div class="announcement-bar">a</div><div class="promo-bar-wrap">b</div></body>')
    assert (s["pages_with_named_region"], s["pages_with_fallback_region_only"]) == (1, 0)


def test_a_bar_inside_the_window_of_a_long_page_is_not_past_the_budget():
    filler = "<p>filler</p>" * (dcc._BODY_BUDGET // 13 + 4_000)
    s = _diag_stats(f'<body><div class="announcement-bar">x</div>{filler}</body>')
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"]) == (1, 0)


def test_the_past_budget_search_stops_at_its_limit(monkeypatch):
    monkeypatch.setattr(dcc, "_DIAG_SCAN_LIMIT", 1_000)
    filler = "<p>filler</p>" * (dcc._BODY_BUDGET // 13 + 600)
    s = _diag_stats(f'<body>{filler}' + "<p>more</p>" * 500 + '<div class="announcement-bar">x</div></body>')
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"]) == (1, 0)


def test_a_trailing_underscore_does_not_turn_a_captured_code_into_a_miss():
    s = _diag_stats('<body><div class="announcement-bar">Use code SAVE10_ today</div></body>')
    assert (s["codes_seen"], s["by_miss"]) == (1, {})


_FILLER = "<p>filler</p>" * (dcc._BODY_BUDGET // 13 + 4_000)  # past the body window


@pytest.mark.parametrize(
    "late",
    [
        '<script>el.id = "announcement-bar"; el.className="x";</script>',
        '<style>[class="top-bar"]{color:red}</style>',
        '<div data-section-id="announcement-bar">x</div>',
        '<pre>class="announcement-bar"</pre>',
        '<p>id = "announcement-bar"</p>',
        '<pre>see class="announcement-bar" in the theme</pre>',
    ],
)
def test_a_region_name_outside_a_class_or_id_attribute_is_not_past_the_budget(late):
    s = _diag_stats(f"<body>{_FILLER}{late}</body>")
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"]) == (1, 0)


@pytest.mark.parametrize(
    "late",
    [
        '<div id="announcement-bar">x</div>',
        "<div class='shopify-section announcement-bar'>x</div>",
        '<div data-x="1" class="top-bar">x</div>',
        '<div\n    class = "announcement-bar">x</div>',
        '<div title="a"id="announcement-bar">x</div>',
    ],
)
def test_a_region_name_in_a_late_class_or_id_attribute_is_past_the_budget(late):
    s = _diag_stats(f"<body>{_FILLER}{late}</body>")
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"]) == (1, 1)


# A tag in the last `_WINDOW_EDGE` chars counts even when its (short) text was read: it may be cut.
@pytest.mark.parametrize("before_end,read", [(6, 0), (20, 0), (40, 0), (299, 1)])
def test_a_bar_cut_at_the_window_edge_is_past_the_budget(before_end, read):
    tag = '<div class="announcement-bar">'
    html = "<body>" + "x" * (dcc._BODY_BUDGET - before_end) + tag + "Use code EDGE10</div>" + "y" * 1_000 + "</body>"
    assert dcc._body_start(html) == 0
    s = _diag_stats(html)
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"], s["codes_seen"]) == (1, 1, read)


def test_a_bar_read_whole_before_the_window_edge_is_not_past_the_budget():
    tag = '<div class="announcement-bar">'
    html = "<body>" + "x" * (dcc._BODY_BUDGET - 400) + tag + "Use code EDGE10</div>" + "y" * 1_000 + "</body>"
    s = _diag_stats(html)
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"], s["codes_seen"]) == (1, 0, 1)


def test_a_bar_near_the_end_of_a_page_inside_the_budget_is_not_past_it():
    html = "<body>" + "x" * (dcc._BODY_BUDGET - 200) + '<div class="announcement-bar">Use code EDGE10</div></body>'
    assert len(html) - dcc._body_start(html) <= dcc._BODY_BUDGET
    s = _diag_stats(html)
    assert (s["pages_over_budget"], s["pages_named_region_past_budget"], s["codes_seen"]) == (0, 0, 1)


@pytest.mark.parametrize(
    "phrase",
    [
        "Use\n        code",
        "Enter the\n          discount code",
        "apply\t\tpromo\n   code",
        "Promo code\n      :",
        "use" + " " * 40 + "code",
    ],
)
def test_a_phrase_wrapped_across_source_lines_is_counted_from_the_html(phrase):
    html = f'<html><head><script>var t = "{phrase} X";</script></head><body><div class="rte">x</div></body>'
    assert _diag_stats(html)["pages_code_phrase_in_html"] == 1


def test_a_phrase_gap_past_forty_blanks_is_not_counted():
    html = '<body><div class="rte">use' + " " * 41 + "code and promo code" + " " * 41 + ":</div></body>"
    assert _diag_stats(html)["pages_code_phrase_in_html"] == 0


def test_the_phrase_search_stops_at_its_limit(monkeypatch):
    monkeypatch.setattr(dcc, "_DIAG_SCAN_LIMIT", 1_000)
    s = _diag_stats("<body>" + "<p>x</p>" * 200 + "<p>Use code LATE10</p></body>")
    assert s["pages_code_phrase_in_html"] == 0
    dcc.reset_stats()
    assert _diag_stats("<body><p>Use code LATE10</p></body>")["pages_code_phrase_in_html"] == 1


def test_strict_codes_past_the_page_cap_are_not_wording_misses():
    codes = [f"SAVE{n}" for n in range(10, 10 + dcc._MAX_CODES_PER_PAGE + 2)]
    text = " ".join(f"Use code {c}." for c in codes) + " Code: NC20."
    s = _diag_stats(f'<body><div class="announcement-bar">{text}</div></body>')
    assert (s["pages_codes_capped"], s["codes_seen"]) == (1, dcc._MAX_CODES_PER_PAGE)
    assert s["by_miss"] == {"past_cap": 2, "wording": 1}


# ---- widened phrases (2026-11-07, from the by_miss "wording" diagnostics) --------------------------------------


@pytest.mark.parametrize(
    "bar,code,form",
    [
        ("Get 15% off with code SAVE15", "SAVE15", "with_code"),
        ("Free shipping using promo code SHIPFREE", "SHIPFREE", "with_code"),
        ("20% off with the discount code: SPRING20", "SPRING20", "labelled"),  # labelled reads it first
        ("15% off with the code SAVE15", "SAVE15", "with_code"),
        ("Extra 20% off sitewide - code SPRING20", "SPRING20", "after_code"),
        ("Code: WELCOME15 for 15% off your order", "WELCOME15", "after_code"),
        ("CODE WEEKEND25 | 25% OFF", "WEEKEND25", "after_code"),
        ("Use code SAVE10 for 10% off", "SAVE10", "use_code"),  # an older form keeps its label
        ("Promo code: VIP", "VIP", "labelled"),
        ("Use code NC2B for 10% off", "NC2B", "use_code"),
    ],
)
def test_the_widened_forms_read_codes_the_strict_ones_missed(bar, code, form):
    got = _codes(f'<body><div class="announcement-bar">{bar}</div></body>')
    assert code in got, got
    assert got[code].form == form


@pytest.mark.parametrize(
    "bar",
    [
        "Discount code required at checkout",  # lowercase prose is never a code
        "Enter your ZIP CODE 90210 to see delivery",  # no letter
        "SEE CODE BELOW",  # a stop word
        "CODESALE starts now",  # "code" must be a whole word
        "CODEWORD20 is our collection",  # the same, with a tail no stop word would refuse
        "Use code NC20 shade for a match",  # a shade
        "Scan the QR code ABOVE",  # a stop word
        "Gift card code: 1234-5678-ABCD-9999",  # a gift-card number
        "Code: NC20",  # a shade-like short token after a bare "code"
        "code N25 matches",  # the same
        # The review of be#2732's false hits for the bare form, each in a bar that also says "% off" or "sale":
        "Product code: AB1234. Sale ends Sunday",
        "Shade code 2N1 - 20% off all foundations",
        "Code: 2N1 for 20% off",  # a digit-first token
        "YOUR CODE WILL BE EMAILED - 15% off first order",
        "Discount code CANNOT be combined. 10% off sale items",
        "Promo code WORKS on full-price items, save 10%",
        "Postal code SW1A1AA ships free - free shipping over $50",
        "Your referral code FRIEND10 gives 10% off to a friend",
        "Text code JOIN to 12345 for 10% off",
        "Style code: FW23-118 now 30% off",
        "Dress code BLACK for the 20% off party",
        "Item code: QX4410 on sale",
        "Code FALL23 applies",  # no promo context at all
        "Use code XXXX-XXXX for 10% off",  # a dashed placeholder
        "barcode A1B2C3 on 20% off items",  # "code" inside a word
        "Unicode U8FF20 sale",  # the same, after a word no noun rule names
        "Code BLACK takes 20% off",  # a colour word a bare "code" names
    ],
)
def test_the_widened_forms_do_not_read_prose_shades_or_gift_cards(bar):
    assert _codes(f'<body><div class="announcement-bar">{bar}</div></body>') == {}


def test_the_widened_form_reads_terms_like_the_others():
    got = _codes('<body><div class="announcement-bar">Get 15% off orders over $50 with code SAVE15</div></body>')
    c = got["SAVE15"]
    assert (c.kind, c.value, c.min_subtotal) == ("percent", "15", "50")


def test_a_wording_miss_is_now_a_capture():
    html = '<body><div class="announcement-bar">Get 15% off with code SAVE15</div></body>'
    codes = dcc.capture_shadow(html, url="https://glow.example/p", market="US", page_currency="USD")
    assert [c.code for c in codes] == ["SAVE15"]
    s = dcc.stats()
    assert s["codes_seen"] == 1
    assert s["by_form"] == {"with_code": 1}
    assert "wording" not in s["by_miss"]


def test_the_window_now_reaches_a_bar_past_200k_chars():
    assert dcc._BODY_BUDGET == 500_000
    html = "<body>" + "x" * 300_000 + '<div class="announcement-bar">Use code DEEP10</div></body>'
    assert "DEEP10" in _codes(html)


def test_capture_shadow_returns_the_codes_and_nothing_on_failure(monkeypatch):
    got = dcc.capture_shadow('<body><div class="announcement-bar">Use code GLOW20</div></body>',
                             url="https://a.example", market="US", page_currency="USD")
    assert [c.code for c in got] == ["GLOW20"]
    monkeypatch.setattr(dcc, "_scan", lambda html: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dcc.capture_shadow("<body></body>", url="https://a.example", market="US", page_currency="USD") == []
    assert dcc.stats()["errors"] == 1


def test_the_write_flag_defaults_off(monkeypatch):
    from config.settings import settings

    monkeypatch.delenv("DEAL_CODE_CAPTURE_WRITE", raising=False)
    assert settings.deal_code_capture_write is False
    for v in ("1", "yes", "on"):  # only "true" turns a DEAL_* flag on
        monkeypatch.setenv("DEAL_CODE_CAPTURE_WRITE", v)
        assert settings.deal_code_capture_write is False
    monkeypatch.setenv("DEAL_CODE_CAPTURE_WRITE", "TRUE")
    assert settings.deal_code_capture_write is True


@pytest.mark.parametrize("shadow,write,expect_write", [
    ("true", "true", True), ("true", None, False), (None, "true", False), ("true", "false", False),
])
def test_the_page_refresh_records_codes_only_with_both_flags(monkeypatch, shadow, write, expect_write):
    import services.external_offers_service as svc

    for name, value in (("DEAL_CODE_CAPTURE_SHADOW", shadow), ("DEAL_CODE_CAPTURE_WRITE", write)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    page = '<html><head></head><body><div class="announcement-bar">Use code GLOW20 for 20% off</div></body></html>'
    recorded = []

    async def fake_fetch_html(url, **kwargs):
        if kwargs.get("observed") is not None:
            kwargs["observed"]["final_url"] = "https://www.brand.example/products/toner?variant=1"
        return page, "text/html"

    async def no_existing(*a, **k):
        return None

    async def spy_record(codes, **kwargs):
        recorded.append(([c.code for c in codes], kwargs))
        raise RuntimeError("stop after the hook")

    monkeypatch.setattr(svc, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(svc, "_get_snapshot_row", no_existing)
    monkeypatch.setattr(svc.deal_code_capture_writer, "record", spy_record)

    async def _boom(*a, **k):
        raise RuntimeError("no database here")

    monkeypatch.setattr(svc.database, "execute", _boom)
    monkeypatch.setattr(svc.database, "fetch_one", _boom)
    try:
        asyncio.run(svc.resolve_external_offer(market="us", url="https://brand.example/products/toner", force_refresh=True))
    except Exception:
        pass
    if expect_write:
        assert len(recorded) == 1
        codes, kwargs = recorded[0]
        assert codes == ["GLOW20"]
        assert (kwargs["url"], kwargs["market"]) == ("https://brand.example/products/toner", "US")
        assert kwargs["final_url"] == "https://www.brand.example/products/toner?variant=1"  # where the fetch ended
    else:
        assert recorded == []



def test_a_false_bare_code_no_longer_steals_the_real_codes_terms():
    got = _codes('<body><div class="announcement-bar">Item code: QX4410 | 10% off with code SAVE10</div></body>')
    assert set(got) == {"SAVE10"}
    assert (got["SAVE10"].kind, got["SAVE10"].value) == ("percent", "10")


def test_the_bare_form_reads_a_code_only_with_a_promo_word_on_either_side():
    assert "FALL23" in _codes('<body><div class="announcement-bar">Fall sale: code FALL23</div></body>')
    assert "FALL23" in _codes('<body><div class="announcement-bar">Code FALL23 takes 20% off</div></body>')
    assert "FALL23" not in _codes('<body><div class="announcement-bar">Code FALL23 applies</div></body>')


def test_a_minimum_spend_keeps_its_own_currency_symbol():
    got = _codes('<body><div class="announcement-bar">20% off orders over £50 with code SAVE20</div></body>')
    assert (got["SAVE20"].min_subtotal, got["SAVE20"].min_subtotal_symbol) == ("50", "£")



# ---- block boundaries (review of be#2735, P3-N5) ------------------------------------------------------------------


@pytest.mark.parametrize("html", [
    '<div class="announcement-bar"><div>20% OFF USE CODE</div><div>48HOURS ONLY</div></div>',
    '<div class="announcement-bar"><p>Take 15% off with code</p><p>SPRING SALE NOW</p></div>',
    '<div class="announcement-bar"><li>Promo code:</li><li>SAVE20 EXCLUDED ITEMS</li></div>',
])
def test_a_code_phrase_never_reaches_into_the_next_block(html):
    assert _codes(html) == {}


def test_a_line_break_inside_the_phrase_words_still_reads_the_code():
    assert set(_codes('<div class="announcement-bar">Use promo<br>code SAVE15 for 15% off</div>')) == {"SAVE15"}


# ---- void and form tags inside one line of a bar (review of be#2735 delta 3, P3-R2) -------------------------------


@pytest.mark.parametrize("html", [
    '<div class="announcement-bar">Promo code:<br>SAVE15 for 15% off</div>',
    '<div class="announcement-bar">Use code<br>SAVE15 for 15% off</div>',
    '<div class="announcement-bar">Use code<br/>SAVE15 for 15% off</div>',
    '<div class="announcement-bar">Use code <button>SAVE15</button> for 15% off</div>',
    '<div class="announcement-bar">Use code <label>SAVE15</label> for 15% off</div>',
    '<div class="announcement-bar">Use code <wbr>SAVE15 for 15% off</div>',
    '<div class="announcement-bar">Use code <input type="hidden" value="x"> SAVE15 for 15% off</div>',
    '<div class="announcement-bar">Use code <select><option>SAVE15</option></select> for 15% off</div>',
])
def test_a_void_or_form_tag_between_the_phrase_and_the_code_still_reads(html):
    assert set(_codes(html)) == {"SAVE15"}


def test_a_copy_code_button_inside_a_marquee_item_still_reads_and_items_stay_apart():
    html = ('<div class="announcement-bar"><div>20% OFF USE CODE</div><div>48HOURS ONLY</div>'
            '<div>Use code <button>SAVE15</button> for 15% off</div></div>')
    assert set(_codes(html)) == {"SAVE15"}


@pytest.mark.parametrize("separator", ['<img src="dot.svg">', '<svg><circle r="1"/></svg>',
                                       '<picture><source srcset="a.webp"><img src="a.png"></picture>'])
def test_an_icon_between_inline_marquee_items_keeps_them_apart(separator):
    """Review of be#2742, P3-2: images are the separators between inline marquee items, so they stay a boundary."""
    html = (f'<div class="announcement-bar"><span>FREE SHIPPING OVER $50 USE CODE</span>{separator}'
            '<span>SS24 COLLECTION 15% OFF</span></div>')
    assert _codes(html) == {}


def test_a_line_break_in_a_fallback_region_still_reads_the_code():
    assert set(_codes('<body><div class="promo-bar-wrap">Promo code:<br>SAVE15 for 15% off</div></body>')) == {"SAVE15"}

