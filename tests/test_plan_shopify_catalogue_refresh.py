"""The catalogue lane's read-only dry run (`scripts/ops/plan_shopify_catalogue_refresh.py`): its gates and plan.

The end-to-end run (a real database, the lane's real reader over a mock storefront) is in
tests/test_external_seed_catalogue_refresh_postgres.py. Here: every gate closes BEFORE any query or fetch,
and the plan reads seeds with the lane's own rule.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timezone
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
        "PIVOTA_ENV", "CATALOGUE_PLAN_EGRESS", "DATABASE_URL", "DB_STATEMENT_TIMEOUT_SECONDS", "TOP_HOSTS")}
    return subprocess.run([sys.executable, str(SCRIPT)], env={**base, **env}, capture_output=True, text=True,
                          timeout=120)


def test_the_file_has_no_at_sign():
    assert "@" not in SCRIPT.read_text(), "run_oneoff_job.sh picks its --args delimiter from a fixed set"


@pytest.mark.parametrize("env,reason", [
    ({}, "PIVOTA_ENV is not production"),
    ({"PIVOTA_ENV": "production"}, "CATALOGUE_PLAN_EGRESS must state pivota-crawl"),
    ({"PIVOTA_ENV": "production", "CATALOGUE_PLAN_EGRESS": "default"}, "CATALOGUE_PLAN_EGRESS must state pivota-crawl"),
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


def test_a_host_line_counts_matches_reasons_and_samples_disagreements():
    mod = _module()
    from tests.test_external_seed_catalogue_refresh import _read_host

    catalogue = _read_host([[_product(p50="90.00")]], wanted=(HANDLE, "gone", "x"))
    seed = {**_drain_row(), "snapshot_variants": None}
    seed["variants"] = seed["seed_data"]["variants"]
    gone = {**seed, "id": "s-gone", "destination_url": f"https://{HOST}/products/gone"}
    line = mod.plan_host(HOST, catalogue, [seed, gone])
    assert (line["seeds"], line["matched"]) == (2, 1)
    assert line["reasons"] == {"read": 1, "handle_not_read": 1}
    assert line["price"] == {"moved_over_10pct": 1} and line["sample"][0]["seed_id"] == seed["id"]
    assert line["status"] == "ok" and line["proof"] == "base_and_page_cookie"
