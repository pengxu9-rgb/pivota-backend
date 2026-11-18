"""Reviewed seed batch with the REAL canonical writers, on the production dialect.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs `tests/test_*_postgres.py`).

🚨 PRIVATE DATABASE, as in tests/test_agent_pdp_view_unbuildable_reap_postgres.py: the full schema (every model table,
every SQL migration, the app-code stubs) must not be built in the database the sibling gate files share, and the real
writers use the module-global `database`, so the batch runs in a SUBPROCESS whose DATABASE_URL is the private database
(tests/reviewed_seed_batch_real_writer_driver.py).

What this adds to tests/test_reviewed_seed_batch_postgres.py (whose writers are faked): the real upsert_seed_data,
promote_variants_for_group and refresh_agent_pdp_view_for_seed, wrapped by spies that call straight through, on a
2-entry batch where entry 1 is priced 1299 with SKUs containing "." and "/", and entry 2 has 20 additions. It asserts:
  1. the writers' kwargs equal the reviewed one-seed core's calls, transcribed for these entries (prices included);
  2. both entries commit, so verify_expansion passed against the real promoter's rows;
  3. one connection pid and one transaction id per entry, SERIALIZABLE, shared by all of that entry's writers;
  4. each seed_data_proposals row is merged and carries the batch's labels;
  5. the real rollback restores both entries' exact preimages;
  6. a failure raised after a real nested writer committed its savepoint rolls back the whole entry.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "tests" / "reviewed_seed_batch_real_writer_driver.py"

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)


def _private_url(name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{name}"))


def _admin(sql: str, *params):
    import psycopg2

    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(sql, params or None)
    finally:
        admin.close()


def _drive(scenario: str, tmp_path) -> dict:
    name = f"sb_real_writers_{scenario}_{os.getpid()}"
    _admin(f'DROP DATABASE IF EXISTS "{name}"')
    _admin(f'CREATE DATABASE "{name}"')
    try:
        env = {key: value for key, value in os.environ.items()}
        env.update({"DATABASE_URL": _private_url(name), "TZ": "UTC", "PIVOTA_ENV": "test"})
        for step in (["build_schema", "x"], [scenario, str(tmp_path)]):
            run = subprocess.run([sys.executable, str(DRIVER), *step], cwd=REPO_ROOT, env=env, capture_output=True,
                                 text=True, timeout=600)
            assert run.returncode == 0, run.stderr[-3000:]
            line = next(l for l in run.stdout.splitlines() if l.startswith("DRIVER_RESULT "))
        return json.loads(line[len("DRIVER_RESULT "):])
    finally:
        _admin("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
               name)
        _admin(f'DROP DATABASE IF EXISTS "{name}"')


def _driver_module():
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import reviewed_seed_batch_real_writer_driver as driver

    return driver


def _reference_calls(raw, before_variants, mode):
    """The reviewed one-seed core's writer calls (2026-11-18), transcribed for this entry,
    with its price where the core had the constant 14.0. Audit labels are compared separately."""
    url, price = raw["listing"]["url"], raw["listing"]["price_minor"] / 100
    added = [{"variant_id": n, "title": shade, "sku": sku, "barcode": gtin[-13:], "option_name": "SHADE",
              "option_value": shade, "url": f"{url}?variant={n}", "price_amount": price, "price_currency": "USD",
              "availability": "in_stock", "in_stock": True} for n, gtin, shade, sku in raw["additions"]]
    array = before_variants + added if mode == "apply" else before_variants
    calls = [("upsert_seed_data", {"seed_id": raw["seed_id"], "external_product_id": raw["product_key"].rsplit("::", 1)[1],
                                   "proposed_seed_data": {"variants": array}, "project_after_merge": False})]
    if mode == "apply":
        calls.append(("promote_variants_for_group", {"group_id": raw["group_id"], "apply": True,
                                                     "variant_ids": sorted(n for n, *_ in raw["additions"])}))
    calls.append(("refresh_agent_pdp_view_for_seed", {"seed_id": raw["seed_id"]}))
    return calls


LABELS = {"upsert_seed_data": {"proposer", "source", "notes"},
          "refresh_agent_pdp_view_for_seed": {"refresh_source", "proposal_id"}}


@pytest.fixture(scope="module")
def round_trip(tmp_path_factory):
    return _drive("round_trip", tmp_path_factory.mktemp("round_trip"))


def test_real_writers_commit_both_entries_and_roll_back_to_the_exact_preimages(round_trip):
    driver = _driver_module()
    assert round_trip["plan_code"] == 0 and round_trip["apply_code"] == 0, round_trip.get("apply_rows")
    statuses = [(r.get("seed_id"), r["status"]) for r in round_trip["apply_rows"]]
    seeds = [raw["seed_id"] for raw in driver.ENTRIES]
    assert statuses == [(seeds[0], "committed"), (seeds[1], "committed"), (None, "committed")]
    receipts = [r["receipt"] for r in round_trip["apply_rows"] if r["kind"] == "entry"]
    assert [(r["variants"], r["sku_and_offer_count"]) for r in receipts] == [(4, 3), (21, 20)]
    assert [round_trip["after"][s] for s in seeds] == [r["postimage_sha256"] for r in receipts]
    # Real offers at each entry's own price.
    assert round_trip["offers"][seeds[0]] == [12.99] * 3 and round_trip["offers"][seeds[1]] == [14.0] * 20
    assert round_trip["rollback_code"] == 0
    assert [(r.get("seed_id"), r["status"]) for r in round_trip["rollback_rows"]] == [
        (seeds[1], "committed"), (seeds[0], "committed"), (None, "committed")]
    assert round_trip["restored"] == round_trip["before"]


def test_real_writer_arguments_match_the_reviewed_core(round_trip):
    driver = _driver_module()
    for spied, mode, order in ((round_trip["apply_spied"], "apply", driver.ENTRIES),
                               (round_trip["rollback_spied"], "rollback", list(reversed(driver.ENTRIES)))):
        expected = []
        for raw in order:
            before = [{"variant_id": n, "title": shade, "sku": sku, "barcode": gtin[-13:], "option_name": "SHADE",
                       "option_value": shade, "url": f"{raw['listing']['url']}?variant={n}",
                       "price_amount": raw["listing"]["price_minor"] / 100, "price_currency": "USD",
                       "availability": "in_stock", "in_stock": True} for n, gtin, shade, sku in raw["existing"]]
            expected += _reference_calls(raw, before, mode)
        assert [s["writer"] for s in spied] == [name for name, _ in expected]
        for record, (name, reference) in zip(spied, expected):
            got = {k: v for k, v in record["kwargs"].items() if k not in LABELS.get(name, set())}
            assert got == reference, name
            assert LABELS.get(name, set()) <= record["kwargs"].keys()


def test_each_entry_is_one_serializable_transaction_on_one_connection(round_trip):
    driver = _driver_module()
    for spied in (round_trip["apply_spied"], round_trip["rollback_spied"]):
        by_seed = {}
        for record in spied:
            by_seed.setdefault(record["seed_id"], set()).add((record["txid"], record["pid"]))
            assert record["iso"] == "serializable"
        assert set(by_seed) == {raw["seed_id"] for raw in driver.ENTRIES}
        assert all(len(ids) == 1 for ids in by_seed.values()), by_seed
        assert len({next(iter(ids))[0] for ids in by_seed.values()}) == len(by_seed), "entries shared a transaction"


def test_every_proposal_is_merged_with_the_batch_labels(round_trip):
    from services import reviewed_seed_batch as batch

    proposals = round_trip["proposals"]
    assert len(proposals) == 4 and all(p["status"] == "merged" for p in proposals)
    assert all((p["proposer"], p["source"]) == (batch.PROPOSER, batch.OPERATION) for p in proposals)
    assert [json.loads(p["notes"])["operation"] for p in proposals] == ["apply", "apply", "rollback", "rollback"]


def test_a_failure_after_a_real_nested_writer_rolls_back_the_whole_entry(tmp_path):
    driver = _driver_module()
    result = _drive("nested_failure", tmp_path)
    seeds = [raw["seed_id"] for raw in driver.ENTRIES]
    assert result["apply_code"] == 2
    assert [(r.get("seed_id"), r["status"]) for r in result["apply_rows"]] == [
        (seeds[0], "committed"), (seeds[1], "rolled_back"), (None, "stopped")]
    assert "injected failure after the real promote_variants_for_group" in result["apply_rows"][1]["error"]
    assert result["after"][seeds[0]] != result["before"][seeds[0]]
    assert result["after"][seeds[1]] == result["before"][seeds[1]]
    assert result["apply_rows"][1]["read_back"]["state_sha256"] == result["before"][seeds[1]]
    assert [p["seed_id"] for p in result["proposals_after_apply"]] == [seeds[0]]
