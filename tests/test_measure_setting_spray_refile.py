"""scripts/ops/measure_setting_spray_refile.py without a database: its regex selects what the classifier
files on the leaf, its summary counts moves by domain and shelf, and its drift line takes out only the
leaf's own entries. The SQL itself runs in tests/test_measure_setting_spray_refile_postgres.py."""
import importlib.util
import re
from pathlib import Path

import pytest

from services.pdp_category_classifier import classify

_PATH = Path(__file__).resolve().parent.parent / "scripts/ops/measure_setting_spray_refile.py"
_spec = importlib.util.spec_from_file_location("measure_setting_spray_refile", _PATH)
measure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure)

LEAF = "beauty/makeup/face/setting-spray"


def _as_python(pg_regex: str) -> "re.Pattern[str]":
    return re.compile(pg_regex.replace(r"\y", r"\b"), re.I)


@pytest.mark.parametrize("text", [
    "Makeup Fixing Mist", "You Mist Makeup-Extending Setting Spray", "Jill Stuart Crystal Glow & Fix Mist",
    "Mist & Fix Matte Spray", "3CE Makeup Fixer Mist", "Hera Make Up Fixer 80ml", "espoir Fresh Setting Fixer 100ml",
    "All Nighter Setting Spray",
    # merchant type / category spellings the first version's type regex missed (narrow review of #2547)
    "Fixing Mist", "Fix Mist 50ml", "Mist & Fix", "Makeup Fixing Spray", "Make-Up Setting Fixer", "Setting Fixers",
])
def test_every_text_the_classifier_files_on_the_leaf_is_a_candidate(text):
    assert classify(text)[1] == LEAF
    assert _as_python(measure.candidate_regex()).search(text)


def test_the_candidate_regex_is_the_classifiers_own_in_postgres_dialect():
    from services.pdp_category_classifier import SETTING_SPRAY_PHRASES

    regex = measure.candidate_regex()
    assert r"\b" not in regex and regex.startswith(r"\y(") and regex.endswith(r")\y")
    assert SETTING_SPRAY_PHRASES in regex


def test_every_printed_line_is_text_and_the_count_closes_the_run():
    out = measure.render([{"kind": "precondition", "check": "x", "ok": True}, "GATE_OPEN",
                          {"kind": "key", "product_key": "k"}, {"kind": "summary"}])
    assert out[0].startswith("MEASURE {") and out[1] == "GATE_OPEN" and out[2].startswith("MEASURE_KEY {")
    assert out[-2] == "GATE_OPEN" and out[-1] == f"MEASURE_DONE lines={len(out) - 1}"
    assert all(not line.startswith("{") for line in out)


def test_a_failed_run_never_reopens_the_gate_but_still_counts():
    out = measure.render([{"kind": "precondition", "check": "x", "ok": True}, "GATE_OPEN",
                          {"kind": "error", "error": "boom"}])
    assert out.count("GATE_OPEN") == 1 and out[-1] == f"MEASURE_DONE lines={len(out) - 1}"
    assert measure.render([{"kind": "error", "error": "no url"}]) == [
        'MEASURE {"error": "no url", "kind": "error"}', "MEASURE_DONE lines=1"]


def _row(key, path, title, ptype="", domain="shop.example", serving=True):
    return {"product_key": key, "category_path": path, "category": None, "product_type": ptype, "title": title,
            "source_domain": domain, "serving_eligible": serving}


def test_the_summary_counts_moves_by_domain_and_shelf_and_lists_the_moved_keys():
    lines = measure.summarize([
        _row("k_toner", "beauty/skincare/tone/toner", "Makeup Fixing Mist"),
        _row("k_gap", "beauty/makeup/setting-spray", "All Nighter Setting Spray", domain="b.example", serving=False),
        _row("k_powder", "beauty/makeup/face/powder", "Translucent Powder", "Setting Spray and Powder"),
        _row("k_spray", "beauty/makeup/face/powder", "All Nighter Setting Spray", "Setting Spray and Powder"),
        _row("k_plural", "beauty", "Rose Water Facial Mist", "Face Mists & Setting Sprays"),
    ])
    keys = {line["product_key"]: line for line in lines if line["kind"] == "key"}
    assert set(keys) == {"k_toner", "k_gap", "k_spray", "k_plural"}       # the powder stays powder
    # the backfill's resolver reads the title; a drain leaves a blank-type row unresolved ("": left out)
    assert keys["k_toner"]["row"] == LEAF and keys["k_toner"]["feed"] == ""
    assert keys["k_spray"]["shelf"] == "combined_spray_powder" and keys["k_spray"]["feed"] == LEAF
    # a two-product type: the title decides (a face mist is a toner), the drain leaves it unresolved
    assert keys["k_plural"]["shelf"] == "multi_product"
    assert keys["k_plural"]["row"] == "beauty/skincare/tone/toner" and keys["k_plural"]["feed"] == ""
    domains = {(line["source_domain"], line["resolver"], line["shelf"]) for line in lines if line["kind"] == "from_to"}
    assert ("b.example", "feed", "none") in domains and ("shop.example", "feed", "combined_spray_powder") in domains
    [summary] = [line for line in lines if line["kind"] == "summary"]
    assert summary["feed_moved_not_serving"] == 1 and summary["feed_same_serving"] == 1


def test_the_drift_line_takes_out_only_the_leafs_own_entries():
    [line] = measure.drift_lines({"count": 3, "sample_keys": [LEAF, "beauty/other/x"],
                                  "detail": {"in_code_not_in_table": [LEAF, "beauty/other/x"],
                                             "alias_conflicts": ["beauty/makeup/setting-spray: code=a table=b"]}})
    assert line["count_excluding_setting_spray_leaf"] == 1
    assert line["setting_spray_entries"] == sorted([LEAF, "beauty/makeup/setting-spray: code=a table=b"])
