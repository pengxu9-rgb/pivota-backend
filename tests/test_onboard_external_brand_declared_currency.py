"""The crawl onboarder files a price only in its market's currency (ADR-026).

It used to read a missing `price_currency` as USD in three places (seed upsert, the
default variant, dry-run line), whatever the item's market -- so a `--market SG` run
filed SG seeds priced in USD. Now each item must declare `price_currency`, and it must
be `market_registry.currency_for(market)`. A missing one, or one that differs, refuses
the whole run before the publication gate fetches anything and before any write,
dry-run included. It is never defaulted to USD, nor to the market's currency.

Driven through the script's real entry point: `python -m
scripts.onboard_external_brand_from_crawl` in a subprocess for the refusals and the
dry-run, and `main()` in-process for --apply (the DB is stubbed there).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.onboard_external_brand_from_crawl as onboard  # noqa: E402
from services import market_registry  # noqa: E402

MODULE = "scripts.onboard_external_brand_from_crawl"


def _item(epid: str, **extra):
    item = {
        "external_product_id": epid,
        "brand": "Acme",
        "title": f"Product {epid}",
        "category_kind": "skincare",
        "destination_url": f"https://acme.example/products/p-{epid}",
        "price_amount": 10.0,
        "extracted_at": "2026-10-01T00:00:00+00:00",
    }
    item.update(extra)
    return item


def _run(tmp_path, cohort, *args):
    cohort_file = tmp_path / "cohort.json"
    cohort_file.write_text(json.dumps(cohort), encoding="utf-8")
    # --no-publication-gate: an accepted dry run must not reach the network.
    return subprocess.run(
        [sys.executable, "-m", MODULE, "--file", str(cohort_file), "--no-publication-gate", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )


def test_the_registry_currencies_these_tests_assume():
    assert market_registry.currency_for("US") == "USD"
    assert market_registry.currency_for("SG") == "SGD"
    assert market_registry.currency_for("JP") == "JPY"
    assert all(market_registry.MARKETS[m]["ingest"] for m in ("US", "SG", "JP"))


@pytest.mark.parametrize("cohort, args, refusal", [
    # The reported case: --market SG and no price_currency is not an SG seed in USD.
    ([_item("1")], ["--market", "SG"], "'1': CURRENCY_REQUIRED"),
    # Not even for US: a missing currency is never read as USD.
    ([_item("1")], ["--market", "US"], "'1': CURRENCY_REQUIRED"),
    ([_item("1", market="US")], [], "'1': CURRENCY_REQUIRED"),
    ([_item("1", price_currency="  ")], ["--market", "SG"], "'1': CURRENCY_REQUIRED"),
    ([_item("1", price_currency="USD")], ["--market", "SG"], "'1': CURRENCY_MISMATCH:USD!=SGD"),
    ([_item("1", market="jp", price_currency="usd")], [], "'1': CURRENCY_MISMATCH:USD!=JPY"),
    ([_item("1", market="US", price_currency="SGD")], [], "'1': CURRENCY_MISMATCH:SGD!=USD"),
    # One bad item refuses the run: no partial onboard of the rest.
    ([_item("1", price_currency="SGD"), _item("2")], ["--market", "SG"], "'2': CURRENCY_REQUIRED"),
])
def test_entry_point_refuses_a_missing_or_foreign_currency(tmp_path, cohort, args, refusal):
    result = _run(tmp_path, cohort, *args)

    assert result.returncode != 0, result.stdout
    assert f"REFUSED currency: {refusal}" in result.stderr
    assert "refused: 1 currency problem(s)" in result.stderr
    # Refused before the dry-run plan: nothing is listed as onboardable.
    assert "would onboard" not in result.stdout
    assert "DRY ::" not in result.stdout


def test_entry_point_reports_market_and_currency_problems_together(tmp_path):
    cohort = [
        _item("1", market="ZZ"),                       # market refused: no currency check
        _item("2", market="SG"),                        # currency missing
        _item("3", market="SG", price_currency="SGD"),  # fine
    ]
    result = _run(tmp_path, cohort)

    assert result.returncode != 0
    assert "REFUSED market: '1': UNKNOWN_MARKET:ZZ" in result.stderr
    assert "REFUSED currency: '2': CURRENCY_REQUIRED" in result.stderr
    assert "'1': CURRENCY" not in result.stderr
    assert "1 market problem(s)" in result.stderr
    assert "1 currency problem(s)" in result.stderr
    assert "would onboard" not in result.stdout


def test_entry_point_dry_run_lists_the_declared_currency(tmp_path):
    result = _run(
        tmp_path,
        [_item("1", market="SG", price_currency=" sgd "), _item("2", market="jp", price_currency="JPY")],
    )
    assert result.returncode == 0, result.stderr
    lines = {line.split()[2]: line for line in result.stdout.splitlines() if "would onboard" in line}
    assert "10.0 SGD market=SG" in lines["1"]
    assert "10.0 JPY market=JP" in lines["2"]
    assert "USD" not in result.stdout


class _StubDB:
    def __init__(self):
        self.connected = 0
        self.calls = []

    async def connect(self):
        self.connected += 1

    async def disconnect(self):
        pass

    async def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))


@pytest.fixture
def apply_spies(monkeypatch):
    db = _StubDB()
    seen = {"onboarded": None, "publication_gate": 0, "db": db}

    async def _onboard(kept, dropped, *, serve=True, unpublished=None):
        seen["onboarded"] = [dict(p) for p in kept]

    async def _partition(cohort):
        seen["publication_gate"] += 1
        return cohort, [], {}

    monkeypatch.setattr(onboard, "database", db)
    monkeypatch.setattr(onboard, "_onboard", _onboard)
    monkeypatch.setattr(onboard, "partition_by_publication", _partition)
    return seen


def _main(monkeypatch, tmp_path, cohort, *args):
    cohort_file = tmp_path / "cohort.json"
    cohort_file.write_text(json.dumps(cohort), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [MODULE, "--file", str(cohort_file), "--apply", *args])
    return onboard.main()


@pytest.mark.parametrize("cohort", [
    [_item("1", price_currency="SGD"), _item("2")],
    [_item("1", price_currency="SGD"), _item("2", price_currency="USD")],
])
def test_apply_refusal_happens_before_the_publication_gate_and_the_db(
    monkeypatch, tmp_path, apply_spies, cohort
):
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, tmp_path, cohort, "--market", "SG")
    assert "refused: 1 currency problem" in str(exc.value.code)
    assert apply_spies["publication_gate"] == 0
    assert apply_spies["db"].connected == 0
    assert apply_spies["onboarded"] is None


async def test_apply_writes_the_market_currency_on_seed_and_variant(monkeypatch, tmp_path, apply_spies):
    # main() runs its own event loop; drive it from a worker thread.
    rc = await asyncio.to_thread(
        _main, monkeypatch, tmp_path,
        [_item("1", price_currency="sgd"), _item("2", market="sg", price_currency=" SGD ")],
        "--market", "SG",
    )
    assert rc == 0
    kept = apply_spies["onboarded"]
    assert [p["price_currency"] for p in kept] == ["SGD", "SGD"]

    # The writer binds exactly the stamped currency, on the row and on its variant.
    db = apply_spies["db"]
    for p in kept:
        await onboard._upsert_seed(p, seller_ref=None, seed_kind=None)
    seeds = [params for sql, params in db.calls if "INSERT INTO external_product_seeds" in sql]
    assert [s["currency"] for s in seeds] == ["SGD", "SGD"]
    assert [s["market"] for s in seeds] == ["SG", "SG"]
    variants = [json.loads(s["data"])["snapshot"]["variants"][0] for s in seeds]
    assert [v["currency"] for v in variants] == ["SGD", "SGD"]


@pytest.mark.parametrize("currency, refusal", [
    (None, "CURRENCY_REQUIRED"),
    ("", "CURRENCY_REQUIRED"),
    ("USD", "CURRENCY_MISMATCH:USD!=SGD"),
])
async def test_the_seed_writer_refuses_on_its_own_past_the_run_guard(monkeypatch, currency, refusal):
    # A caller that skips resolve_cohort_currencies still cannot file SG in USD.
    db = _StubDB()
    monkeypatch.setattr(onboard, "database", db)
    p = _item("1", market="SG")
    if currency is not None:
        p["price_currency"] = currency
    with pytest.raises(ValueError, match=refusal):
        await onboard._upsert_seed(p, seller_ref=None, seed_kind=None)
    assert db.calls == []


def test_the_default_variant_never_invents_usd():
    # scripts/backfill_crawl_seed_variants.py builds variants from stored rows, which
    # carry no market; a row with no currency stays without one rather than USD.
    assert onboard.build_default_seed_variant(_item("1"))["currency"] is None
    assert onboard.build_default_seed_variant(_item("1", price_currency=" jpy "))["currency"] == "JPY"
