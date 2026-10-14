"""The crawl onboarder files a seed only under a DECLARED, ingested market (ADR-026).

It used to read a missing `market` as US in three places (seed upsert, offer relabel,
dry-run line). Now each cohort item declares its own market or the run declares one
with --market; a missing market, one the registry does not list, one it does not
ingest, and an item market that contradicts --market each refuse the whole run before
the publication gate fetches anything and before any write, dry-run included.

Driven through the script's real entry point: `python -m
scripts.onboard_external_brand_from_crawl` in a subprocess for the refusals and the
dry-run, and `main()` in-process for --apply (the DB is stubbed there).
"""

from __future__ import annotations

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


def _off_market() -> str:
    return next(code for code in market_registry.MARKETS if not market_registry.MARKETS[code]["ingest"])


def test_the_registry_still_has_an_off_market_for_these_tests_to_use():
    assert _off_market()
    assert "ZZ" not in market_registry.MARKETS


@pytest.mark.parametrize("cohort, args, refusal", [
    ([_item("1")], [], "'1': MARKET_REQUIRED"),
    ([_item("1", market="")], [], "'1': MARKET_REQUIRED"),
    ([_item("1", market="ZZ")], [], "'1': UNKNOWN_MARKET:ZZ"),
    ([_item("1", market="OFF")], [], "'1': MARKET_NOT_INGESTED:OFF"),
    ([_item("1")], ["--market", "ZZ"], "--market: UNKNOWN_MARKET:ZZ"),
    ([_item("1")], ["--market", "OFF"], "--market: MARKET_NOT_INGESTED:OFF"),
    ([_item("1", market="JP")], ["--market", "US"], "'1': MARKET_CONFLICT:JP!=US"),
    # One bad item refuses the run: no partial onboard of the rest.
    ([_item("1", market="US"), _item("2")], [], "'2': MARKET_REQUIRED"),
])
def test_entry_point_refuses_an_undeclared_or_unservable_market(tmp_path, cohort, args, refusal):
    off = _off_market()
    cohort = [dict(p, market=off) if p.get("market") == "OFF" else p for p in cohort]
    args = [off if a == "OFF" else a for a in args]
    refusal = refusal.replace("OFF", off)

    result = _run(tmp_path, cohort, *args)

    assert result.returncode != 0, result.stdout
    assert f"REFUSED market: {refusal}" in result.stderr
    assert "refused:" in result.stderr
    # Refused before the dry-run plan: nothing is listed as onboardable.
    assert "would onboard" not in result.stdout
    assert "DRY ::" not in result.stdout


def test_entry_point_never_files_an_undeclared_item_under_us(tmp_path):
    result = _run(tmp_path, [_item("1")])
    assert result.returncode != 0
    assert "market=US" not in result.stdout


def test_entry_point_run_market_declares_items_without_their_own(tmp_path):
    result = _run(tmp_path, [_item("1"), _item("2", market=" sg ")], "--market", "sg")
    assert result.returncode == 0, result.stderr
    assert "would onboard 1 " in result.stdout
    assert result.stdout.count("market=SG") == 2
    assert "market=US" not in result.stdout


def test_entry_point_per_item_markets_without_a_run_market(tmp_path):
    result = _run(tmp_path, [_item("1", market="US"), _item("2", market="jp")])
    assert result.returncode == 0, result.stderr
    lines = {line.split()[2]: line for line in result.stdout.splitlines() if "would onboard" in line}
    assert "market=US" in lines["1"]
    assert "market=JP" in lines["2"]


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


def test_apply_refusal_happens_before_the_publication_gate_and_the_db(monkeypatch, tmp_path, apply_spies):
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, tmp_path, [_item("1", market="US"), _item("2")])
    assert "refused: 1 market problem" in str(exc.value.code)
    assert apply_spies["publication_gate"] == 0
    assert apply_spies["db"].connected == 0
    assert apply_spies["onboarded"] is None


async def test_apply_writes_the_declared_market_on_seed_and_offer(monkeypatch, tmp_path, apply_spies):
    # main() runs its own event loop; drive it from a worker thread.
    import asyncio

    rc = await asyncio.to_thread(
        _main, monkeypatch, tmp_path, [_item("1"), _item("2", market="sg")], "--market", "SG"
    )
    assert rc == 0
    kept = apply_spies["onboarded"]
    assert [p["market"] for p in kept] == ["SG", "SG"]

    # The writers bind exactly the stamped market.
    db = apply_spies["db"]
    for p in kept:
        await onboard._upsert_seed(p, seller_ref=None, seed_kind=None)
        await onboard._set_category_and_offer(p, "pk::" + p["external_product_id"], "merch_obs_x")
    seed_markets = [params["market"] for sql, params in db.calls if "INSERT INTO external_product_seeds" in sql]
    offer_markets = [params["market"] for sql, params in db.calls if "UPDATE catalog_offers" in sql]
    assert seed_markets == ["SG", "SG"]
    assert offer_markets == ["SG", "SG"]
