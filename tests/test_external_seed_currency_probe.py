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
    snapshot), and its only database access is the census's READ ONLY `collect` (--from-db)."""
    text = (_ROOT / "scripts/ops/external_seed_currency_probe.py").read_text()
    assert "resolve_external_offer(" not in text
    assert "from db." not in text and "import database" not in text
    assert ".execute(" not in text and ".fetch(" not in text
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE "):
        assert verb not in text
    assert "census.collect(conn)" in text


# ------------------------------------------------------------------ pacing, deadline, start window

class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.fetch_times: list = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_requests_are_paced_globally_not_just_per_host() -> None:
    """The crawl NAT trips a cross-domain 429 on ~50 requests over 37 domains in a minute, so
    the interval holds ACROSS hosts, whatever the host concurrency."""
    probe, clock = _probe(), _FakeClock()

    async def fetch(url: str) -> str:
        clock.fetch_times.append(clock.t)
        return PAGES.get(url) or _html()

    sample = [dict(e, urls=e["urls"][:2]) for e in SAMPLE]
    asyncio.run(probe.probe(sample, fetch, host_concurrency=4, global_interval=1.5, clock=clock, sleep=clock.sleep))
    times = sorted(clock.fetch_times)
    assert len(times) == sum(len(e["urls"]) for e in sample)
    assert all(b - a >= 1.5 - 1e-9 for a, b in zip(times, times[1:])), times


def test_past_the_deadline_no_fetch_starts_and_the_rest_are_not_attempted() -> None:
    probe, clock = _probe(), _FakeClock()
    fetched = []

    async def fetch(url: str) -> str:
        fetched.append(url)
        return PAGES.get(url) or _html()

    obs = asyncio.run(probe.probe(SAMPLE, fetch, host_concurrency=1, global_interval=1.0, deadline=2.5,
                                  clock=clock, sleep=clock.sleep))
    assert len(fetched) == 3  # at t=0, 1, 2; the next would start at 3 >= 2.5
    classes = [o["class"] for key in obs for o in obs[key]]
    assert classes.count("not_attempted") == sum(len(e["urls"]) for e in SAMPLE) - 3
    summary = probe.summarise(SAMPLE, obs)
    # fetched: nocur p1, p2 and named p1. An unread page is in no share: mixed.example and
    # down.example read nothing, so they have no estimate; named.example's one read page counts.
    by_host = {h["host"]: h for h in summary["hosts"]}
    assert by_host["nocur.example"]["est_rows_no_currency"] == 40
    assert by_host["named.example"]["est_rows_no_currency"] == 0
    assert by_host["named.example"]["not_attempted"] == 1
    assert by_host["mixed.example"]["est_rows_no_currency"] is None
    assert by_host["mixed.example"]["not_attempted"] == 4


@pytest.mark.parametrize(
    "hhmm, refused",
    [((4, 14), False), ((4, 15), True), ((5, 15), True), ((6, 14), True), ((6, 15), False), ((12, 0), False)],
)
def test_the_no_start_window_covers_the_refresh_and_this_runs_deadline(hhmm, refused) -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 9, 29, *hhmm, tzinfo=timezone.utc)
    assert (_probe().start_refusal(now) is not None) is refused


def test_from_db_refuses_inside_the_window_before_touching_the_database(monkeypatch, capsys) -> None:
    from datetime import datetime, timezone

    probe = _probe()

    async def boom(dsn):  # pragma: no cover - must not be reached
        raise AssertionError("the database was read inside the no-start window")

    monkeypatch.setattr(probe, "sample_from_db", boom)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    rc = asyncio.run(probe._main(["--from-db"], now=datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)))
    assert rc == 2
    assert "refusing" in capsys.readouterr().err


def test_from_db_and_a_sample_file_are_exclusive() -> None:
    with pytest.raises(SystemExit):
        asyncio.run(_probe()._main(["sample.json", "--from-db"]))
    with pytest.raises(SystemExit):
        asyncio.run(_probe()._main([]))


@pytest.mark.parametrize("census_fails", [False, True])
def test_the_database_connection_is_closed_before_any_fetch(monkeypatch, census_fails) -> None:
    """The crawl can run 45 minutes; it must never hold a session on the 2-vCPU primary."""
    import sys
    import types

    probe = _probe()
    conns = []

    class FakeConn:
        closed = False

        async def close(self):
            self.closed = True

    async def connect(dsn):
        conns.append(FakeConn())
        return conns[-1]

    class FakeCensus:
        @staticmethod
        async def collect(conn):
            if census_fails:
                raise RuntimeError("statement timeout")
            return {"totals": {}}, [], SAMPLE[:1]

        @staticmethod
        def render(summary):
            return []

        @staticmethod
        def output_lines(summary, suspects, sample):
            return ["CENSUS_JSON {}"]

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(probe, "_census_module", lambda: FakeCensus)
    if census_fails:
        with pytest.raises(RuntimeError):
            asyncio.run(probe.sample_from_db("postgresql://x/y"))
    else:
        assert asyncio.run(probe.sample_from_db("postgresql://x/y")) == SAMPLE[:1]
    assert conns and conns[0].closed
