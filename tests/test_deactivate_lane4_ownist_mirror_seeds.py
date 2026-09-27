"""Preconditions of scripts/deactivate_lane4_ownist_mirror_seeds.py.

The script is a prod one-off; what must not regress is WHICH seeds it will touch. A seed is
deactivated only while its product still carries the lane-4 tombstone and the seed is still
attached to that product. Anything else is a refusal, and one refusal stops the whole run.
"""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "deactivate_lane4_ownist_mirror_seeds.py"
_spec = importlib.util.spec_from_file_location("deactivate_lane4_ownist_mirror_seeds", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_TS = datetime(2026, 7, 10, 4, 2, 32, tzinfo=timezone.utc)


def _world(**overrides):
    seeds = {
        sid: {
            "id": sid, "status": "active", "domain": "ownist.com", "title": "t",
            "canonical_url": "https://ownist.com/products/x", "attached_product_key": key,
            "updated_at": _TS,
        }
        for sid, key in mod.SEEDS.items()
    }
    products = {
        key: {
            "product_key": key, "suppressed_at": _TS, "suppression_reason": mod.CUT_REASON,
            # jsonb as asyncpg returns it without a codec: TEXT.
            "suppression_metadata": json.dumps({"script": mod.CUT_SCRIPT, "run_id": "20260710T040232Z"}),
        }
        for key in mod.SEEDS.values()
    }
    return seeds, products


def test_all_four_are_deactivated_when_the_tombstone_stands():
    seeds, products = _world()
    to_deactivate, already, refusals = mod.classify(seeds, products)
    assert sorted(s["id"] for s in to_deactivate) == sorted(mod.SEEDS)
    assert already == [] and refusals == []


def test_metadata_as_a_dict_is_read_too():
    seeds, products = _world()
    for p in products.values():
        p["suppression_metadata"] = {"script": mod.CUT_SCRIPT}
    assert len(mod.classify(seeds, products)[0]) == 4


def test_a_reverted_tombstone_is_refused():
    seeds, products = _world()
    key = next(iter(mod.SEEDS.values()))
    products[key].update(suppressed_at=None, suppression_reason=None, suppression_metadata=None)
    to_deactivate, _, refusals = mod.classify(seeds, products)
    assert len(refusals) == 1 and "lane-4 tombstone" in refusals[0]
    assert len(to_deactivate) == 3  # the caller aborts on any refusal; see plan_and_apply


def test_a_product_suppressed_by_another_script_is_refused():
    seeds, products = _world()
    key = next(iter(mod.SEEDS.values()))
    products[key]["suppression_metadata"] = json.dumps({"script": "some_other_cut"})
    assert len(mod.classify(seeds, products)[2]) == 1


def test_a_repointed_seed_is_refused():
    seeds, products = _world()
    sid = next(iter(mod.SEEDS))
    seeds[sid]["attached_product_key"] = "prod::somewhere::else"
    refusals = mod.classify(seeds, products)[2]
    assert len(refusals) == 1 and "re-pointed" in refusals[0]


def test_a_missing_seed_or_product_is_refused():
    seeds, products = _world()
    sid, key = next(iter(mod.SEEDS.items()))
    del seeds[sid]
    assert len(mod.classify(seeds, products)[2]) == 1
    seeds, products = _world()
    del products[key]
    assert len(mod.classify(seeds, products)[2]) == 1


def test_an_already_inactive_seed_is_not_rewritten():
    seeds, products = _world()
    sid = next(iter(mod.SEEDS))
    seeds[sid]["status"] = "inactive"
    to_deactivate, already, refusals = mod.classify(seeds, products)
    assert already == [sid] and len(to_deactivate) == 3 and refusals == []


def test_the_manifest_carries_what_revert_needs():
    seeds, products = _world()
    manifest = mod.build_manifest(mod.classify(seeds, products)[0])
    assert {s["id"] for s in manifest["seeds"]} == set(mod.SEEDS)
    for s in manifest["seeds"]:
        assert s["prior_status"] == "active"
        assert s["attached_product_key"] == mod.SEEDS[s["id"]]
    json.dumps(manifest)  # printable as one log line


def test_the_writes_never_touch_attached_product_key_and_recheck_it():
    for sql in (mod.DEACTIVATE_SQL, mod.REACTIVATE_SQL):
        set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
        assert "attached_product_key" not in set_clause
        assert "attached_product_key = :key" in sql.split("WHERE", 1)[1]


def test_the_program_can_ship_inline_through_run_oneoff_job():
    assert "@" not in _PATH.read_text()
