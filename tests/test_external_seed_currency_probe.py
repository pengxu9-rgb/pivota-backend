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


def _census():
    spec = importlib.util.spec_from_file_location(
        "external_seed_price_parse_census", _ROOT / "scripts/ops/external_seed_price_parse_census.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _big_sample(hosts: int = 1200):
    """The shape of prod's 397-host sample (one line of it was cut at 102,400 chars), scaled up."""
    return [
        {"host": f"shop-{i:04d}.example.com", "market": "US", "served": 5, "default_shaped": 5,
         "default_shaped_attached_offers": 9,
         "urls": [f"https://shop-{i:04d}.example.com/products/a-long-product-handle-{j}-" + "x" * 60 for j in range(3)]}
        for i in range(hosts)
    ]


def test_every_census_output_line_stays_under_the_log_cap_and_parses_back() -> None:
    census = _census()
    sample = _big_sample()
    suspects = [{"id": f"eps_{i}", "host": "h.example", "flags": ["peer_x100"] * 3} for i in range(3000)]
    summary = {"totals": {"served": 1}, "top_hosts": [{"host": f"h{i}", "n": i} for i in range(40)]}
    lines = census.output_lines(summary, suspects, sample)
    assert max(len(line) for line in lines) < census.MAX_LINE_CHARS < 102_400
    sample_lines = [ln for ln in lines if ln.startswith("CURRENCY_SAMPLE_JSON_PART ")]
    assert len(sample_lines) > 1, "the fixture must actually need splitting"
    # the probe reads the census output as-is, parts shuffled, with a log prefix on each line
    text = "\n".join(f"2026-09-28T06:26:00Z {ln}" for ln in reversed(lines))
    assert _probe().load_sample(text) == sample
    suspect_parts = [json.loads(ln.split(" ", 2)[2]) for ln in lines if ln.startswith("SUSPECTS_JSON_PART ")]
    assert [s for part in suspect_parts for s in part] == suspects


def test_a_missing_or_repeated_part_is_refused() -> None:
    census, probe = _census(), _probe()
    lines = census.json_part_lines("CURRENCY_SAMPLE_JSON", _big_sample())
    assert len(lines) >= 3
    with pytest.raises(SystemExit, match="incomplete"):
        probe.load_sample("\n".join(lines[:1] + lines[2:]))
    with pytest.raises(SystemExit, match="twice"):
        probe.load_sample("\n".join(lines + lines[:1]))


def test_an_item_too_big_for_any_line_is_an_error_not_an_overlong_line() -> None:
    with pytest.raises(ValueError, match="line cap"):
        _census().json_part_lines("CURRENCY_SAMPLE_JSON", [{"urls": ["x" * 100_000]}])


def test_an_oversized_census_line_is_trimmed_not_cut() -> None:
    census = _census()
    summary = {"totals": {}, "top_hosts": [{"host": "x" * 5000} for _ in range(40)]}
    (line,) = [ln for ln in census.output_lines(summary, [], []) if ln.startswith("CENSUS_JSON ")]
    assert len(line) < census.MAX_LINE_CHARS
    assert json.loads(line[len("CENSUS_JSON "):])["truncated_top_hosts"] is True


def test_the_probe_output_stays_under_the_cap() -> None:
    probe = _probe()
    sample = _big_sample()
    summary = probe.summarise(sample, {})
    lines = probe.output_lines(summary)
    assert lines[0].startswith("PROBE_JSON ") and len(lines) > 2
    assert max(len(line) for line in lines) < _census().MAX_LINE_CHARS


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
