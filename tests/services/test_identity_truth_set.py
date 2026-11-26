"""The identity truth set (services/identity_truth_set.py). Synthetic data only: placeholder brands
and example.* hosts, never a real store or product."""

import json
import os
import re

import pytest

from scripts.ops import build_identity_truth_set as cli
from services import identity_truth_set as ts

REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def g13(body12: str) -> str:
    s = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body12))
    return body12 + str((10 - s % 10) % 10)


G1, G2, G3 = g13("880100000001"), g13("880100000002"), g13("880100000003")


def row(key, host, title, gtin, brand="Brand Alpha", **kw):
    return {"listing_key": key, "host": host, "brand": brand, "title": title, "gtin": gtin,
            "price": 20.0, "currency": "USD", **kw}


def by_basis(pairs):
    out = {}
    for p in pairs:
        out.setdefault(p.basis, []).append(p)
    return out


# --- tier-3 fixture --------------------------------------------------------------------------

def _group(gid, label, source, titles, **extra):
    return {"group_id": gid, "label": label, "source": source, **extra,
            "rows": [{"brand": "Brand Alpha", "domain": f"store-{i}.example", "title": t,
                      "url": f"https://store-{i}.example/p/{gid}"} for i, t in enumerate(titles)]}


def test_tier3_fixture_maps_each_label_to_the_identity_question():
    fixture = {"groups": [
        _group("g1", "collapse", "lane3_as_applied", ["Glow Serum 30ml", "Glow Serum 30 ml"]),
        _group("g2", "keep_separate", "KEEP_size_variants", ["Glow Serum 30ml", "Glow Serum 50ml"]),
        _group("g3", "keep_separate", "KEEP_title_collision_distinct_products", ["Cream", "Cream"]),
        _group("g4", "collapse", "KEEP_title_collision_distinct_products", ["Mask 4", "Mask 4"], relabeled=True),
        _group("g5", "keep_separate", "KEEP_multi_seller_observation", ["Toner 150ml", "Toner 150 ml"]),
        _group("g6", "keep_separate", "KEEP_regional_storefronts", ["Soap 120g", "Soap 120g"]),
        _group("g7", "keep_separate", "SOMETHING_NEW", ["A", "B"]),
    ]}
    pairs, counts = ts.from_tier3_fixture(fixture)
    got = {p.source: (p.label, p.grade, p.basis) for p in pairs}
    assert got == {
        "tier3:g1": ("same", "gold", "tier3_collapse"),
        "tier3:g2": ("different", "gold", "tier3_size_variants"),
        "tier3:g3": ("different", "gold", "tier3_distinct_products"),
        "tier3:g4": ("same", "gold", "tier3_collapse"),
        "tier3:g5": ("same", "inferred", "tier3_kept_rows_same_product"),
        "tier3:g6": ("same", "inferred", "tier3_kept_rows_same_product"),
    }
    assert counts["skipped:keep_separate:SOMETHING_NEW"] == 1


def test_tier3_large_group_is_capped():
    fixture = {"groups": [_group("big", "collapse", "lane3_as_applied", [f"Serum {i}" for i in range(13)])]}
    pairs, _ = ts.from_tier3_fixture(fixture)
    assert len(pairs) == ts.MAX_PAIRS_PER_GROUP


# --- owner pair labels -----------------------------------------------------------------------

def _page(key, pid, lt="Glow Serum 30ml", at="Glow Serum 30 ml"):
    side = lambda h, t: {"title": t, "brand": "Brand Alpha", "price": 20, "cur": "USD", "host": h,  # noqa: E731
                         "url": f"https://{h}/p/{key}", "img": f"https://{h}/i/{key}.jpg", "size": "30 ml"}
    return {"key": key, "pid": pid, "tier": "T0c", "L": side("store-a.example", lt), "A": side("store-b.example", at),
            "g": {"v": "different"}}


def test_owner_labels_map_and_skip_unsure_reverted_and_orphans():
    page = [_page("pk_1", "irp_1"), _page("pk_2", "irp_2"), _page("pk_3", "irp_3"), _page("pk_4", "irp_bad")]
    labels = [{"pair_key": "pk_1", "label": "correct", "proposal_id": "irp_1"},
              {"pair_key": "pk_2", "label": "wrong", "proposal_id": "irp_2"},
              {"pair_key": "pk_3", "label": "unsure", "proposal_id": "irp_3"},
              {"pair_key": "pk_4", "label": "correct", "proposal_id": "irp_bad"},
              {"pair_key": "pk_9", "label": "correct"}]
    pairs, counts = ts.from_owner_pair_labels(page, labels, reverted_proposals=["irp_bad"])
    assert [(p.source, p.label, p.grade, p.basis) for p in pairs] == [
        ("owner:pk_1", "same", "gold", "owner_pair_label"), ("owner:pk_2", "different", "gold", "owner_pair_label")]
    assert counts["skipped_label:unsure"] == 1 and counts["skipped_reverted_proposal"] == 1
    assert counts["label_without_page_pair"] == 1
    assert pairs[0].a["image_url"].endswith(".jpg") and '"v"' not in json.dumps(ts.to_row(pairs[0]))


# --- barcode rows ----------------------------------------------------------------------------

def test_clean_barcode_group_gives_one_same_pair_across_hosts():
    pairs, counts = ts.from_barcode_rows([
        row("k1", "store-a.example", "Brand Alpha Glow Serum 30ml", G1),
        row("k2", "store-b.example", "Glow Serum 30 ml", "0" + G1),  # another spelling of the barcode
    ])
    assert [(p.label, p.grade, p.basis) for p in pairs] == [("same", "silver", "barcode_clean")]
    assert counts["barcode_clean"] == 1


def test_one_host_and_two_brands_give_no_label():
    pairs, counts = ts.from_barcode_rows([
        row("k1", "store-a.example", "Glow Serum 30ml", G1),
        row("k2", "store-a.example", "Glow Serum 30ml", G1),
        row("k3", "store-a.example", "Night Cream 50ml", G2),
        row("k4", "store-b.example", "Night Cream 50ml", G2, brand="Brand Beta"),
    ])
    assert pairs == []
    assert counts["groups_one_host"] == 1 and counts["groups_skipped_brands_disagree"] == 1


def test_polluted_barcode_gives_only_different_pairs():
    pairs, counts = ts.from_barcode_rows([
        row("k1", "store-a.example", "Glow Serum 30ml", G1),
        row("k2", "store-b.example", "Glow Serum 50ml", G1),
        row("k3", "store-c.example", "Glow Serum 30ml", G1),  # agrees with k1, but the barcode is unsafe
    ])
    assert {p.label for p in pairs} == {"different"}
    assert {p.basis for p in pairs} == {"barcode_collision"}
    assert len(pairs) == 2 and counts["groups_polluted"] == 1
    assert all("size" in p.note["stated"] for p in pairs)


def test_a_shade_named_on_both_sides_is_a_collision():
    pairs, _ = ts.from_barcode_rows([
        row("k1", "store-a.example", "Lip Tint 4g - Rose", G1),
        row("k2", "store-b.example", "Lip Tint 4g - Coral", G1),
    ])
    assert [(p.label, p.basis) for p in pairs] == [("different", "barcode_collision")]


def test_an_unstated_size_proves_nothing():
    pairs, counts = ts.from_barcode_rows([
        row("k1", "store-a.example", "Glow Serum 30ml", G1),
        row("k2", "store-b.example", "Glow Serum", G1),
    ])
    assert pairs == [] and counts["clean_pairs_unproven"] == 1


def test_invalid_and_restricted_barcodes_are_skipped():
    bad_check = G1[:-1] + str((int(G1[-1]) + 1) % 10)
    restricted = g13("200100000001")  # in-store range
    pairs, counts = ts.from_barcode_rows([
        row("k1", "store-a.example", "Glow Serum 30ml", bad_check),
        row("k2", "store-b.example", "Glow Serum 30ml", bad_check),
        row("k3", "store-a.example", "Glow Serum 30ml", restricted),
        row("k4", "store-b.example", "Glow Serum 30ml", restricted),
    ])
    assert pairs == [] and counts["rows_skipped_no_valid_barcode_or_brand"] == 4


def test_two_clean_barcodes_of_one_line_in_two_sizes_are_different():
    pairs, _ = ts.from_barcode_rows([
        row("k1", "store-a.example", "Glow Serum 30ml", G1),
        row("k2", "store-b.example", "Glow Serum 30 ml", G1),
        row("k3", "store-a.example", "Glow Serum 50ml", G2),
        row("k4", "store-b.example", "Glow Serum 50 ml", G2),
    ])
    groups = by_basis(pairs)
    assert len(groups["barcode_clean"]) == 2
    assert [p.label for p in groups["size_variant"]] == ["different"]


def test_variant_title_is_read_with_the_title():
    pairs, _ = ts.from_barcode_rows([
        row("k1", "store-a.example", "Glow Serum", G1, variant_title="30ml"),
        row("k2", "store-b.example", "Glow Serum 30ml", G1, variant_title="Default Title"),
    ])
    assert [p.basis for p in pairs] == ["barcode_clean"]


# --- merge, judge view, score ----------------------------------------------------------------

def _p(a, b, label, grade="silver", basis="barcode_clean"):
    return ts.TruthPair(pair_id=ts.pair_id(a, b), a=a, b=b, label=label, grade=grade, basis=basis)


A = {"listing_key": "k1", "host": "store-a.example", "title": "Glow Serum 30ml", "gtin": G1}
B = {"listing_key": "k2", "host": "store-b.example", "title": "Glow Serum 30ml", "gtin": G1}
C = {"listing_key": "k3", "host": "store-c.example", "title": "Glow Serum 50ml", "gtin": G2}


def test_pair_id_ignores_order():
    assert ts.pair_id(A, B) == ts.pair_id(B, A)


def test_merge_drops_disagreements_and_keeps_the_higher_grade():
    gold = _p(A, B, "same", grade="gold", basis="tier3_collapse")
    pairs, counts = ts.merge([_p(A, B, "same"), _p(A, C, "same")], [gold, _p(C, A, "different", basis="size_variant")])
    assert [(p.pair_id, p.grade) for p in pairs] == [(gold.pair_id, "gold")]
    assert counts == {"conflicting_labels_dropped": 1, "duplicates_merged": 1}


def test_judge_view_hides_barcode_label_and_keys():
    view = ts.judge_view(_p(A, B, "same"))
    assert set(view) == {"pair_id", "a", "b"}
    assert view["a"] == {"host": "store-a.example", "title": "Glow Serum 30ml"}
    assert "gtin" not in json.dumps(view) and "same" not in json.dumps(view)


def test_score_gate_fails_on_one_wrong_confirmed():
    same, diff = _p(A, B, "same"), _p(A, C, "different", basis="size_variant")
    result = ts.score([same, diff], {same.pair_id: "confirmed", diff.pair_id: "confirmed"})
    assert result["gate"] == "fail" and result["wrong_confirmed"] == [diff.pair_id]
    assert result["by_grade"]["silver"]["confirmed_precision"] == 0.5


def test_score_passes_and_reports_the_rule_of_three():
    same, diff = _p(A, B, "same"), _p(A, C, "different", basis="size_variant")
    result = ts.score([same, diff], {same.pair_id: "likely", diff.pair_id: "not_shown"})
    s = result["by_grade"]["silver"]
    assert result["gate"] == "pass"
    assert (s["same_likely"], s["wrong_confirmed"], s["wrong_confirmed_rate_upper95"]) == (1, 0, 1.0)


def test_score_fails_on_a_missing_verdict_and_refuses_unknown_ones():
    same = _p(A, B, "same")
    assert ts.score([same], {})["gate"] == "fail"
    with pytest.raises(ts.TruthSetError):
        ts.score([same], {same.pair_id: "yes"})


def test_an_inferred_error_does_not_fail_the_gate_but_is_counted():
    p = _p(A, C, "different", grade="inferred", basis="tier3_kept_rows_same_product")
    result = ts.score([p], {p.pair_id: "confirmed"})
    assert result["gate"] == "pass" and result["by_grade"]["inferred"]["wrong_confirmed"] == 1


# --- files and the CLI -----------------------------------------------------------------------

def test_write_refuses_inside_the_repository(tmp_path):
    with pytest.raises(ts.TruthSetError):
        ts.write_jsonl(os.path.join(REPO, "truth_set.jsonl"), [])
    with pytest.raises(ts.TruthSetError):
        ts.write_jsonl(str(tmp_path / "x.jsonl"), [], repo_root=str(tmp_path))
    assert ts.write_jsonl(str(tmp_path / "ok.jsonl"), [{"a": 1}]) == 1


def test_cli_end_to_end_outside_the_repo(tmp_path, capsys):
    log = tmp_path / "run.log"
    log.write_text("\n".join([
        "TRUTHSET_RUN read_only=on",
        "TRUTHSET_ROW " + json.dumps(row("k1", "store-a.example", "Glow Serum 30ml", G1)),
        "TRUTHSET_ROW " + json.dumps(row("k2", "store-b.example", "Glow Serum 30 ml", G1)),
        json.dumps(row("k3", "store-a.example", "Glow Serum 50ml", G2)),
        json.dumps(row("k4", "store-b.example", "Glow Serum 50 ml", G2)),
        "{not json",
        "TRUTHSET_RUN rows=4",
    ]), encoding="utf-8")
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"groups": [_group("g1", "collapse", "lane3_as_applied", ["Mist 100ml", "Mist 100 ml"])]}))
    out = {k: str(tmp_path / f"{k}.jsonl") for k in ("barcode", "tier3", "truth", "judge", "verdicts")}
    assert cli.main(["from-rows", "--rows", str(log), "--out", out["barcode"]]) == 0
    assert cli.main(["from-fixture", "--fixture", str(fixture), "--out", out["tier3"]]) == 0
    assert cli.main(["merge", "--in", out["barcode"], "--in", out["tier3"], "--out", out["truth"]]) == 0
    assert cli.main(["judge-input", "--truth", out["truth"], "--out", out["judge"]]) == 0
    truth = ts.read_jsonl(out["truth"])
    assert len(truth) == 4  # 2 barcode_clean + 1 size_variant + 1 tier3
    judge = ts.read_jsonl(out["judge"])
    assert all("gtin" not in json.dumps(j) and "label" not in j for j in judge)
    with open(out["verdicts"], "w") as fh:
        for t in truth:
            fh.write(json.dumps({"pair_id": t["pair_id"],
                                 "verdict": "confirmed" if t["label"] == "same" else "not_shown"}) + "\n")
    capsys.readouterr()
    assert cli.main(["score", "--truth", out["truth"], "--verdicts", out["verdicts"]]) == 0
    printed = capsys.readouterr().out
    assert '"gate": "pass"' in printed and "Glow" not in printed and "store-a" not in printed


def test_cli_refuses_an_output_inside_the_repo(tmp_path, capsys):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"groups": []}))
    assert cli.main(["from-fixture", "--fixture", str(fixture), "--out", os.path.join(REPO, "t.jsonl")]) == 2
    assert not os.path.exists(os.path.join(REPO, "t.jsonl"))
    assert '"refused"' in capsys.readouterr().out


# --- the read-only SQL -----------------------------------------------------------------------

def test_the_sql_is_one_read_only_select_the_gated_runner_accepts():
    with open(os.path.join(REPO, "scripts", "ops_identity_truth_set_barcode_rows.sql"), encoding="utf-8") as fh:
        sql = fh.read()
    assert re.search(r"^BEGIN READ ONLY;$", sql, re.M) and re.search(r"^COMMIT;$", sql, re.M)
    body = "\n".join(line for line in sql.splitlines() if not re.match(
        r"^\s*(BEGIN READ ONLY|SET LOCAL (statement|lock)_timeout[^;]*|COMMIT);\s*$", line))
    code = "\n".join(line.split("--")[0] for line in body.splitlines())
    assert not re.search(r"^\s*(insert|update|delete|alter|create|drop|truncate|grant|copy|call|vacuum|refresh)\b",
                         code, re.I | re.M)
    assert code.count(";") == 1 and code.strip().endswith(";")
    assert "::text AS row" in code and "o.market = 'US'" in code


def test_at_the_row_cap_the_last_barcode_group_is_dropped():
    rows = [row(f"k{i}", f"store-{i % 2}.example", "Glow Serum 30ml", G1) for i in range(3)]
    rows += [row("k9", "store-a.example", "Glow Serum 30ml", "0" + G2)]
    kept, cut = cli.drop_cut_group(rows, cap=4)
    assert cut and [r["listing_key"] for r in kept] == ["k0", "k1", "k2"]
    assert cli.drop_cut_group(rows, cap=5) == (rows, False)


def _select(path):
    with open(os.path.join(REPO, "scripts", path), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    return lines, lines[lines.index("WITH live AS ("):lines.index("COMMIT;")]


def test_the_data_sql_is_capped_prefixed_and_cross_host():
    lines, select = _select("ops_identity_truth_set_barcode_rows.sql")
    text = "\n".join(select)
    assert select[-1] == "LIMIT 1800;" and str(cli.ROW_CAP) in select[-1]
    assert "'TRUTHSET_ROW ' || json_build_object(" in text and cli.ROW_PREFIX == "TRUTHSET_ROW "
    assert "count(DISTINCT host) >= 2" in text


def test_the_explain_file_plans_exactly_the_data_select():
    data_lines, data_select = _select("ops_identity_truth_set_barcode_rows.sql")
    lines, select = _select("ops_identity_truth_set_barcode_rows_explain.sql")
    assert select == data_select
    assert lines[lines.index("WITH live AS (") - 1] == "EXPLAIN (FORMAT TEXT)"
    assert "ANALYZE" not in "\n".join(lines).upper().replace("NO ANALYZE", "")
    assert "BEGIN READ ONLY;" in lines and lines[-1] == "COMMIT;"
