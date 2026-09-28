"""scripts/ops/external_seed_currency_probe.py sizes what the no-default-currency rule stalls.

Driven through the production extractor with the network stubbed: the classes come from what
`_extract_from_html` + `snapshot_price_fields` make of real page shapes, not from hand-built
extraction dicts.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _probe():
    spec = importlib.util.spec_from_file_location(
        "external_seed_currency_probe", _ROOT / "scripts/ops/external_seed_currency_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _html(price=None, currency=None, jsonld=None):
    head = ['<meta property="og:title" content="Serum">']
    if price is not None:
        head.append(f'<meta property="product:price:amount" content="{price}">')
    if currency is not None:
        head.append(f'<meta property="product:price:currency" content="{currency}">')
    if jsonld is not None:
        head.append(f'<script type="application/ld+json">{json.dumps(jsonld)}</script>')
    return f"<html><head>{''.join(head)}</head><body></body></html>"


PAGES = {
    # nocur.example: every page prices with no currency -> the cohort
    "https://nocur.example/p/1": _html("28.00"),
    "https://nocur.example/p/2": _html("31.50"),
    # named.example: USD stated -> unaffected
    "https://named.example/p/1": _html("28.00", "USD"),
    "https://named.example/p/2": _html(jsonld={"@type": "Product", "name": "x",
                                               "offers": {"price": "9.5", "priceCurrency": "USD"}}),
    # mixed.example (JP): one page states EUR (stored JPY was invented AND wrong), one names none,
    # one has no price, one does not answer
    "https://mixed.example/p/1": _html("2400", "EUR"),
    "https://mixed.example/p/2": _html("2,400"),
    "https://mixed.example/p/3": _html(),
    "https://mixed.example/p/4": None,
}

SAMPLE = [
    {"host": "nocur.example", "market": "US", "served": 40, "default_shaped": 40,
     "default_shaped_attached_offers": 12, "urls": ["https://nocur.example/p/1", "https://nocur.example/p/2"]},
    {"host": "named.example", "market": "US", "served": 90, "default_shaped": 90,
     "default_shaped_attached_offers": 30, "urls": ["https://named.example/p/1", "https://named.example/p/2"]},
    {"host": "mixed.example", "market": "JP", "served": 30, "default_shaped": 30,
     "default_shaped_attached_offers": 6,
     "urls": [f"https://mixed.example/p/{i}" for i in range(1, 5)]},
    {"host": "down.example", "market": "US", "served": 7, "default_shaped": 7,
     "default_shaped_attached_offers": 0, "urls": ["https://down.example/p/1"]},
]


async def _fake_fetch(url: str) -> str:
    page = PAGES.get(url)
    if page is None:
        raise RuntimeError("connection refused")
    return page


def _run():
    probe = _probe()
    observations = asyncio.run(probe.probe(SAMPLE, _fake_fetch))
    return probe, observations, probe.summarise(SAMPLE, observations)


def test_pages_are_classed_by_what_the_extractor_reads() -> None:
    _probe_mod, obs, _summary = _run()
    assert [o["class"] for o in obs["nocur.example|US"]] == ["no_currency", "no_currency"]
    assert [o["class"] for o in obs["named.example|US"]] == ["currency_named", "currency_named"]
    assert [o["class"] for o in obs["mixed.example|JP"]] == [
        "currency_named_other", "no_currency", "no_price", "fetch_failed"
    ]
    assert obs["nocur.example|US"][0]["status"] == "currency_unread"
    assert obs["nocur.example|US"][0]["raw"] == "28.00"


def test_the_estimate_scales_each_hosts_rows_by_its_no_currency_share() -> None:
    _probe_mod, _obs, summary = _run()
    by_host = {h["host"]: h for h in summary["hosts"]}
    assert by_host["nocur.example"]["est_rows_no_currency"] == 40
    assert by_host["nocur.example"]["est_offers_no_currency"] == 12
    assert by_host["nocur.example"]["all_sampled_no_currency"] is True
    assert by_host["named.example"]["est_rows_no_currency"] == 0
    # 1 of 3 pages read (the failed fetch is not a reading) -> a third of 30 rows, of 6 offers
    assert by_host["mixed.example"]["est_rows_no_currency"] == 10
    assert by_host["mixed.example"]["est_offers_no_currency"] == 2
    assert by_host["mixed.example"]["all_sampled_no_currency"] is False
    assert by_host["down.example"]["est_rows_no_currency"] is None

    t = summary["totals"]
    assert (t["est_rows_no_currency"], t["est_offers_no_currency"]) == (50, 14)
    assert (t["hosts_unread"], t["rows_on_unread_hosts"]) == (1, 7)
    assert t["hosts_with_no_currency_pages"] == 2
    assert summary["hosts"][0]["host"] == "nocur.example"


def test_the_census_line_loads_as_is_or_as_bare_json(tmp_path) -> None:
    probe = _probe()
    line = "CURRENCY_SAMPLE_JSON " + json.dumps(SAMPLE)
    assert probe.load_sample(line) == SAMPLE
    assert probe.load_sample(json.dumps(SAMPLE)) == SAMPLE
    with pytest.raises(SystemExit):
        probe.load_sample("{}")


def test_the_report_renders() -> None:
    probe, _obs, summary = _run()
    report = "\n".join(probe.render(summary))
    assert "nocur.example" in report and "est_rows" in report


def test_the_probe_writes_nothing() -> None:
    """It re-reads pages; it must not go through resolve_external_offer (which upserts the
    snapshot) or touch the database."""
    text = (_ROOT / "scripts/ops/external_seed_currency_probe.py").read_text()
    assert "resolve_external_offer(" not in text
    assert "database" not in text.replace("no database", "").replace("the database", "")
