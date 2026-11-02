"""The catalogue lane's read-only dry run (`scripts/ops/plan_shopify_catalogue_refresh.py`): its gates and plan.

The end-to-end run (a real database, the lane's real reader over a mock storefront) is in
tests/test_external_seed_catalogue_refresh_postgres.py. Here: every gate closes BEFORE any query or fetch,
and the plan reads seeds with the lane's own rule.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_external_seed_catalogue_refresh import HOST, HANDLE, _drain_row, _product, _seed_read
# Autouse in its module, and needed here: politeness stubbed (no robots.txt fetch), nothing waits.
from tests.test_external_seed_catalogue_refresh import _no_pacing  # noqa: F401

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/ops/plan_shopify_catalogue_refresh.py"


def _module():
    spec = importlib.util.spec_from_file_location("plan_shopify_catalogue_refresh", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(env):
    base = {k: v for k, v in os.environ.items() if k not in (
        "PIVOTA_ENV", "CATALOGUE_PLAN_EGRESS", "CLOUD_RUN_JOB", "DATABASE_URL", "DB_STATEMENT_TIMEOUT_SECONDS",
        "TOP_HOSTS")}
    return subprocess.run([sys.executable, str(SCRIPT)], env={**base, **env}, capture_output=True, text=True,
                          timeout=120)


def test_the_file_has_no_at_sign():
    assert "@" not in SCRIPT.read_text(), "run_oneoff_job.sh picks its --args delimiter from a fixed set"


@pytest.mark.parametrize("env,reason", [
    ({}, "PIVOTA_ENV is not production"),
    ({"PIVOTA_ENV": "production"}, "CATALOGUE_PLAN_EGRESS must state pivota-crawl"),
    ({"PIVOTA_ENV": "production", "CATALOGUE_PLAN_EGRESS": "default"}, "CATALOGUE_PLAN_EGRESS must state pivota-crawl"),
    # The declared egress alone is a claim a laptop can make; Cloud Run sets CLOUD_RUN_JOB in every job.
    ({"PIVOTA_ENV": "production", "CATALOGUE_PLAN_EGRESS": "pivota-crawl"}, "CLOUD_RUN_JOB unset"),
])
def test_a_gate_closes_before_anything_runs(env, reason):
    proc = _run(env)
    assert proc.returncode == 3, proc.stderr
    assert proc.stdout.startswith("GATE_CLOSED " + reason)
    assert "GATE_OPEN" not in proc.stdout and "SCR_PLAN" not in proc.stdout


@pytest.mark.parametrize("hhmm,quiet", [((2, 59), False), ((3, 0), True), ((5, 15), True), ((6, 29), True),
                                        ((6, 30), False), ((12, 0), False)])
def test_the_sweep_and_refresh_window_is_refused(hhmm, quiet):
    mod = _module()
    assert mod.in_quiet_window(datetime(2026, 11, 1, *hhmm, tzinfo=timezone.utc)) is quiet


def test_the_plan_ranks_hosts_by_catalogue_shaped_seeds():
    mod = _module()
    rows = [{"id": f"a{i}", "destination_url": f"https://a.example/products/p{i}"} for i in range(3)]
    rows += [{"id": f"b{i}", "destination_url": f"https://b.example/products/p{i}"} for i in range(2)]
    rows += [{"id": "c0", "destination_url": "https://c.example/en-gb/products/p"},   # not catalogue-shaped
             {"id": "d0", "destination_url": "https://d.example/p/123"}]
    assert mod.plan_hosts(rows, 5) == {"a.example": ["a0", "a1", "a2"], "b.example": ["b0", "b1"]}
    assert list(mod.plan_hosts(rows, 1)) == ["a.example"]


def test_a_seed_is_compared_with_the_lanes_reading_and_the_real_reconcile():
    mod = _module()
    seed = {**_drain_row(), "snapshot_variants": None}
    seed["variants"] = seed["seed_data"]["variants"]
    agree = mod.compare_seed(seed, _seed_read())
    assert (agree["price"], agree["variants"], agree["variants_changed"]) == ("agree", "all_re_read", 0)
    moved = mod.compare_seed(seed, _seed_read(_product(p50="90.00", p15="32.00")))
    assert (moved["price"], moved["pct"], moved["variants_changed"]) == ("moved_over_10pct", 0.125, 1)
    small = mod.compare_seed(seed, _seed_read(_product(p50="81.00")))
    assert small["price"] == "moved_within_2pct"
    other = mod.compare_seed({**seed, "price_currency": "CAD"}, _seed_read())
    assert other["price"] == "other_currency"
    none = mod.compare_seed({**seed, "price_amount": None}, _seed_read())
    assert none["price"] == "none_stored"
    # The lane's big-move guard, as the refresh applies it: past 1.5x either way the page is read instead.
    assert moved["move_guarded"] is False and agree["move_guarded"] is False
    assert mod.compare_seed(seed, _seed_read(_product(p50="265.00")))["move_guarded"] is True
    assert mod.compare_seed(seed, _seed_read(_product(p50="50.00")))["move_guarded"] is True
    assert none["move_guarded"] is False and other["move_guarded"] is False


def test_a_host_line_counts_matches_reasons_and_samples_disagreements():
    mod = _module()
    from tests.test_external_seed_catalogue_refresh import _read_host

    catalogue = _read_host([[_product(p50="90.00")]], wanted=(HANDLE, "gone", "x"))
    seed = {**_drain_row(), "snapshot_variants": None}
    seed["variants"] = seed["seed_data"]["variants"]
    gone = {**seed, "id": "s-gone", "destination_url": f"https://{HOST}/products/gone"}
    agreeing = {**seed, "id": "s-agree", "price_amount": 90.0}
    line = mod.plan_host(HOST, catalogue, [seed, gone, agreeing])
    assert (line["seeds"], line["matched"]) == (3, 2)
    assert line["reasons"] == {"read": 2, "handle_not_read": 1}
    assert line["price"] == {"moved_over_10pct": 1, "agree": 1}
    # The sample is what a reviewer reads: only the seeds whose price would move.
    assert [s["seed_id"] for s in line["sample"]] == [seed["id"]]
    assert line["status"] == "ok" and line["proof"] == "base_and_page_cookie"


class _Conn:
    """An asyncpg connection as far as `read_only_transaction` asks it anything."""

    def __init__(self, read_only="on", timeout_takes=True):
        self.read_only, self.timeout_takes = read_only, timeout_takes
        self.sql, self.rolled_back, self.readonly_asked = [], 0, None
        self.timeout = "0"

    def transaction(self, readonly=False):
        self.readonly_asked = readonly
        conn = self

        class _Tr:
            async def start(self):
                return None

            async def rollback(self):
                conn.rolled_back += 1

        return _Tr()

    async def execute(self, sql):
        self.sql.append(sql)
        if sql.startswith("SET LOCAL statement_timeout = ") and self.timeout_takes:
            self.timeout = sql.rsplit(" ", 1)[1] + "ms"

    async def fetchval(self, sql):
        return {"SHOW transaction_read_only": self.read_only, "SHOW statement_timeout": self.timeout}[sql]


def test_every_transaction_is_read_only_with_a_statement_timeout():
    mod = _module()
    conn = _Conn()
    asyncio.run(mod.read_only_transaction(conn, 30000))
    assert conn.readonly_asked is True and conn.sql == ["SET LOCAL statement_timeout = 30000"]
    assert conn.rolled_back == 0


@pytest.mark.parametrize("conn,reason", [
    (_Conn(read_only="off"), "transaction is not read-only"),
    (_Conn(timeout_takes=False), "statement_timeout did not take"),
])
def test_a_transaction_that_is_not_read_only_or_timed_closes_the_gate(conn, reason, capsys):
    mod = _module()
    with pytest.raises(SystemExit) as exit_:
        asyncio.run(mod.read_only_transaction(conn, 30000))
    assert exit_.value.code == 3 and conn.rolled_back == 1
    assert capsys.readouterr().out == "GATE_CLOSED " + reason + "\n"


@pytest.mark.parametrize("ratios,expected", [
    ([], {"n": 0, "median": None, "p10": None, "p90": None, "offset_suspected": False}),
    ([1.0, 1.0, 1.0, 1.1], {"n": 4, "median": 1.0, "p10": 1.0, "p90": 1.1, "offset_suspected": False}),
    # Every price 5% up in the store's own currency: a Market's adjustment, not drift.
    ([1.05, 1.05, 1.05, 1.0501, 1.0499], {"n": 5, "median": 1.05, "p10": 1.0499, "p90": 1.0501, "offset_suspected": True}),
    ([0.9, 0.9, 0.91], {"n": 3, "median": 0.9, "p10": 0.9, "p90": 0.91, "offset_suspected": True}),
    ([1.05, 1.05], {"n": 2, "median": 1.05, "p10": 1.05, "p90": 1.05, "offset_suspected": False}),   # too few
    ([0.8, 1.05, 1.05, 1.2], {"n": 4, "median": 1.05, "p10": 0.8, "p90": 1.2, "offset_suspected": False}),  # two-sided
    ([1.005, 1.005, 1.005], {"n": 3, "median": 1.005, "p10": 1.005, "p90": 1.005, "offset_suspected": False}),  # within 1%
    ([None, 1.0], {"n": 1, "median": 1.0, "p10": 1.0, "p90": 1.0, "offset_suspected": False}),
])
def test_a_constant_price_offset_is_told_from_drift(ratios, expected):
    assert _module().price_ratio_stats(ratios) == expected


def test_a_host_line_carries_the_price_ratio_and_the_seeds_due_a_page_read():
    mod = _module()
    from tests.test_external_seed_catalogue_refresh import _read_host

    catalogue = _read_host([[_product(p50="84.00")]], wanted=(HANDLE, "x", "y"))
    seed = {**_drain_row(), "snapshot_variants": None}
    seed["variants"] = seed["seed_data"]["variants"]
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    seeds = [{**seed, "id": f"s{i}", "last_crawled_at": now - timedelta(days=2)} for i in range(3)]
    seeds[0]["last_crawled_at"] = now - timedelta(days=45)
    seeds[1]["destination_verdict"] = "dead"
    line = mod.plan_host(HOST, catalogue, seeds, now=now)
    assert line["price_ratio"] == {"n": 3, "median": 1.05, "p10": 1.05, "p90": 1.05, "offset_suspected": True}
    assert line["page_read_due"] == {"page_read_too_old": 1, "destination_not_live": 1}
    assert line["matched"] == 3, "due seeds are still compared"


def test_a_read_in_a_refused_currency_never_re_prices_a_variant_that_states_none():
    """The refresh's own bar (`_foreign_refresh_variant_currencies`): a USD row takes no CAD read, and a
    stored variant that states no currency would otherwise take the CAD price. The plan bars it too."""
    mod = _module()
    seed = {**_drain_row(), "snapshot_variants": None}
    seed["variants"] = [{k: v for k, v in var.items() if k not in ("price_currency", "currency")}
                        for var in seed["seed_data"]["variants"]]
    compared = mod.compare_seed(seed, _seed_read(base="CAD", cookie="CAD"))
    assert compared["price"] == "other_currency" and compared["variants"] == "not_all_re_read"
    assert compared["variants_changed"] == 0


def test_the_summary_folds_every_host_line_and_names_the_offset_hosts():
    mod = _module()
    from types import SimpleNamespace

    totals = mod.new_totals(2, 6)
    ok = SimpleNamespace(status="ok", proof="base_and_cart")
    offset = {"n": 3, "median": 1.05, "p10": 1.05, "p90": 1.05, "offset_suspected": True}
    mod.fold_host_line(totals, "a.example", ok, {"matched": 3, "move_guarded": 1, "price": {"moved_within_10pct": 3},
                                                 "variants": {"all_re_read": 3}, "page_read_due": {"page_read_too_old": 1},
                                                 "price_ratio": offset})
    mod.fold_host_line(totals, "b.example", SimpleNamespace(status="currency_conflict", proof=None),
                       {"matched": 0, "price": {}, "variants": {}, "page_read_due": {},
                        "price_ratio": {**offset, "n": 0, "offset_suspected": False}})
    assert (totals["hosts_read"], totals["hosts_priceable"], totals["matched"]) == (2, 1, 3)
    assert totals["move_guarded"] == 1, "a host line without the count adds none"
    assert totals["host_status"] == {"ok": 1, "currency_conflict": 1} and totals["proof"] == {"base_and_cart": 1}
    assert totals["page_read_due"] == {"page_read_too_old": 1}
    assert totals["offset_suspected_hosts"] == [{"host": "a.example", **offset}]


def test_the_go_no_go_figures():
    mod = _module()
    totals = {"matched": 50, "variants": {"all_re_read": 49, "not_all_re_read": 1},
              "price": {"agree": 45, "moved_over_10pct": 5}, "offset_suspected_hosts": [{"host": "a"}],
              "proof": {"base_and_page_cookie": 3, "base_and_cart": 1}}
    assert mod.go_no_go({**totals, "move_guarded": 2}) == {
        "not_all_re_read_share": 0.02, "moved_over_10pct_share": 0.1, "move_guarded_share": 0.04,
        "offset_suspected_hosts": 1, "hosts_priced_on_cart_proof": 1}
    assert mod.go_no_go(totals)["move_guarded_share"] == 0.0
    assert mod.go_no_go({**totals, "matched": 0})["not_all_re_read_share"] is None


def test_a_host_line_counts_the_moves_the_job_would_guard_and_only_those(monkeypatch):
    """Like the job: a seed due its page read never reaches the guard, so it is not counted; the ratio is the env's."""
    mod = _module()
    from tests.test_external_seed_catalogue_refresh import _read_host
    from services import external_seed_catalogue_refresh as lane

    monkeypatch.delenv(lane.MAX_PRICE_RATIO_ENV, raising=False)
    catalogue = _read_host([[_product(p50="130.00")]], wanted=(HANDLE, "x", "y"))  # 80 -> 130: past 1.5x
    seed = {**_drain_row(), "snapshot_variants": None}
    seed["variants"] = seed["seed_data"]["variants"]
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    fresh = [{**seed, "id": f"s{i}", "last_crawled_at": now - timedelta(days=2)} for i in range(2)]
    due = {**seed, "id": "s-due", "last_crawled_at": now - timedelta(days=45)}
    line = mod.plan_host(HOST, catalogue, [*fresh, due], now=now)
    assert line["matched"] == 3 and line["move_guarded"] == 2, "the due seed is read from its page anyway"
    # A wider ratio (the env) lets the same move through.
    monkeypatch.setenv(lane.MAX_PRICE_RATIO_ENV, "2")
    assert mod.plan_host(HOST, catalogue, [*fresh, due], now=now)["move_guarded"] == 0
