"""cross_merchant_same_size (M4): proposal selection bound to reviewed content. Pure -- no database.

A hand-labelled subset of a dry run's proposals is the only thing a --write-proposals run may insert.
Each selected proposal is named "<proposal_id>=<content fingerprint>": the fingerprint (cfp_) covers
every (moved listing, anchor) pair as the audit rows show it, so a proposal whose content changed
since review is refused. The selection is folded into the cmd_ digest: a subset's digest never
authorises the full set, another subset, or the same subset reached the other way round."""
from __future__ import annotations

import asyncio
import json
import random
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import identity_cross_merchant_match as cmm  # noqa: E402
from services.identity_cross_merchant_match import (  # noqa: E402
    CONTENT_FINGERPRINT_RE,
    PROPOSAL_ID_RE,
    SelectionError,
    Snapshot,
    content_fingerprint,
    content_fingerprint_payload,
    content_fingerprints_from_audit_rows,
    js_fixed,
    normalise_proposal_ids,
    plan_market,
    proposal_set_digest,
    select_proposals,
)
from test_identity_cross_merchant_match import _mixed, brand_store, retailer  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
AUDIT_FIXTURE = FIXTURES / "m4_audit_rows_ohui_etude_2026_10_17.jsonl"
AUDIT_FINGERPRINTS = FIXTURES / "m4_audit_rows_ohui_etude_2026_10_17_fingerprints.json"


def _ids(plan):
    return [p["proposal_id"] for p in plan.proposals]


def _entries(plan, ids):
    cfps = plan.content_fingerprints()
    return {i: cfps[i] for i in ids}


def _tok(plan, ids):
    return ",".join(f"{i}={fp}" for i, fp in _entries(plan, ids).items())


def _moved(plan):
    return {k for p in plan.proposals
            for k in (p["evidence"].get("from_product_keys") or [p["evidence"]["listing_product_key"]])}


def _selected(mode, ids, cat=None):
    plan = (cat or _mixed()).plan()
    plan.select(mode, _entries(plan, ids))
    return plan


# --- the digest ------------------------------------------------------------------------------------------


def test_a_subsets_digest_differs_from_the_full_sets_and_from_another_subsets():
    full = _mixed().plan()
    ids = _ids(full)
    assert len(ids) == 6
    a, b = ids[:4], ids[2:]
    d_full = full.digest()
    d_a = _selected("only", a).digest()
    d_b = _selected("only", b).digest()
    assert len({d_full, d_a, d_b}) == 3
    assert all(d.startswith("cmd_") for d in (d_a, d_b))
    assert _selected("only", list(reversed(a))).digest() == d_a          # deterministic, order-free
    excl = _selected("exclude", ids[4:])                                 # the same four, the other way
    assert _ids(excl) == a and excl.digest() != d_a
    assert proposal_set_digest(_selected("only", a).proposals) != d_a    # the marker is folded in
    assert d_full == proposal_set_digest(full.proposals)                 # an unselected digest is unchanged


def test_the_exclude_marker_is_folded_into_the_digest():
    full = _mixed().plan()
    excl = _selected("exclude", _ids(full)[4:])
    assert excl.selection[0] == "exclude"
    assert excl.digest() != proposal_set_digest(excl.proposals)                       # no marker
    assert excl.digest() != proposal_set_digest(excl.proposals, selection=("only", excl.selection[1]))
    assert excl.digest() == proposal_set_digest(excl.proposals, selection=excl.selection)


def test_the_digest_folds_the_selected_entries_themselves():
    plan = _mixed().plan()
    ids = _ids(plan)
    cfps = plan.content_fingerprints()
    props = plan.proposals[:2]
    ent = [f"{i}={cfps[i]}" for i in ids[:2]]
    d = proposal_set_digest(props, selection=("only", ent))
    assert proposal_set_digest(props, selection=("only", list(reversed(ent)))) == d
    assert proposal_set_digest(props, selection=("only", [*ent, f"{ids[2]}={cfps[ids[2]]}"])) != d
    assert proposal_set_digest(props, selection=("only", [ent[0], f"{ids[1]}=cfp_{'0' * 24}"])) != d
    assert proposal_set_digest(props, selection=("exclude", ent)) != d


def test_proposal_ids_are_deterministic_across_runs_over_the_same_inputs():
    """proposal_id = irp_ + sha256(proposal_key)[:32]; the key is strategy, merchant, target content_key
    and the move's member fingerprint -- no clock, no run id, no input order."""
    one, two = _mixed().plan(), _mixed().plan()          # two independently built catalogs
    assert _ids(one) == _ids(two) and one.digest() == two.digest()
    assert one.content_fingerprints() == two.content_fingerprints()
    assert [p["proposal_key"] for p in one.proposals] == [p["proposal_key"] for p in two.proposals]
    snap = _mixed().snapshot()
    shuffled = Snapshot(products=random.Random(5).sample(list(snap.products), len(snap.products)),
                        offers=list(reversed(snap.offers)), skus=snap.skus, ck_rows=snap.ck_rows,
                        memberships=snap.memberships, group_sizes=snap.group_sizes)
    assert _ids(plan_market(shuffled, "US")) == _ids(one)
    assert all(PROPOSAL_ID_RE.fullmatch(i) for i in _ids(one))


def test_the_id_pins_only_the_move_and_the_digest_and_fingerprint_pin_the_content():
    """The id derives from the subjects (moved listing + anchor) and the move's keys: a different anchor
    or listing is a different id. What changes under one id is the evidence (prices, sizes, titles):
    proposal_set_digest's per-proposal line carries proposal_fingerprint (kind, strategy, keeper,
    target content_key, subjects, evidence) and the content fingerprint covers the reviewed fields."""
    cat = _mixed()
    plan = cat.plan()
    a = _ids(plan)[:3]
    before = {p["proposal_id"]: p for p in plan.proposals}
    cfp_before = plan.content_fingerprints()
    d_a = _selected("only", a, cat).digest()
    cat.offers[1]["price"] = 25        # one selected retailer's price: same pair, new evidence
    again = cat.plan()
    assert _ids(again) == list(before)                     # every id unchanged
    changed = [p["proposal_id"] for p in again.proposals if p != before[p["proposal_id"]]]
    assert len(changed) == 1 and changed[0] in a
    q, old = next(p for p in again.proposals if p["proposal_id"] == changed[0]), before[changed[0]]
    assert {k for k in q if q[k] != old[k]} == {"evidence"}
    cfp_now = again.content_fingerprints()
    assert [i for i in cfp_now if cfp_now[i] != cfp_before[i]] == changed
    assert _selected("only", a, cat).digest() != d_a
    rest = [i for i in a if i != changed[0]]
    assert _selected("only", rest).digest() == _selected("only", rest, cat).digest()


def test_the_selection_marker_binds_content_the_evidence_does_not_carry():
    """The evidence carries no canonical_url, so proposal_fingerprint does not move when a listing's URL
    does; the content fingerprint in the selection marker does -- a subset digest is bound to it."""
    cat = _mixed()
    ids = _ids(cat.plan())
    reviewed = _selected("only", ids[:3], cat)
    # captured now: the fixture's snapshot shares row dicts with `cat`, and fingerprints are computed lazily
    reviewed_digest, before = reviewed.digest(), reviewed.content_fingerprints()
    bare_before = proposal_set_digest(reviewed.proposals)
    victim = next(p for p in cat.products if p["product_key"] in _moved(reviewed))
    victim["canonical_url"] = "https://moved.example/p/1"
    now = cat.plan()
    assert _ids(now) == ids
    assert proposal_set_digest(now.proposals[:3]) == bare_before                  # the evidence did not move
    after = _entries(now, ids[:3])
    assert len([i for i in before if before[i] != after[i]]) == 1
    now.select("only", after)
    assert now.digest() != reviewed_digest


def test_an_unknown_selection_mode_is_refused():
    plan = _mixed().plan()
    with pytest.raises(SelectionError):
        proposal_set_digest(plan.proposals, selection=("some", _ids(plan)))
    with pytest.raises(SelectionError):
        select_proposals(plan.proposals, "some", _ids(plan)[:1])


# --- the content fingerprint: spec, test vectors, JS parity --------------------------------------------------


def _fixture_rows():
    return [json.loads(line) for line in AUDIT_FIXTURE.read_text(encoding="utf-8").splitlines()]


FIXED_VECTOR_ID = "irp_0b2d7b83a3078d1b99e7a8eba24aabb6"
FIXED_VECTOR_PAYLOAD = (
    '[{"kind":"attach_membership","tier":"T0c","listing":{"product_key":"ext:retailer:036efd8085d808ef3cf823f78f6f1bd9",'
    '"canonical_url":"https://luxiface.com/products/etudehouse-sebum-soak-pact-9-5g",'
    '"title":"[Etudehouse] Sebum soak Pact 9.5g","size":{"value":"9.5000","unit":"g","pack":"1.0000"},'
    '"price":"10.80","currency":"USD"},"anchor":{"product_key":"ext:retailer:0c83f5e3be814ba150edf8144414ec84",'
    '"canonical_url":"https://dodoskin.com/products/etude-sebum-soak-pact-9-5g","title":"ETUDE Sebum Soak Pact 9.5g",'
    '"size":{"value":"9.5000","unit":"g","pack":"1.0000"},"price":"10.81","currency":"USD"}}]')
FIXED_VECTOR_CFP = "cfp_c3fbc49b5a2728331f9f6de5"


def test_the_fixed_test_vector_from_a_real_audit_line():
    """irp_0b2d... from the 2026-10-17 US ohui/etude dry run (one audit line): the exact canonical bytes
    and the fingerprint the review page's JavaScript must produce."""
    rows = [r for r in _fixture_rows() if r["proposal_id"] == FIXED_VECTOR_ID]
    assert len(rows) == 1
    assert content_fingerprint_payload(rows) == FIXED_VECTOR_PAYLOAD
    assert content_fingerprint(rows) == FIXED_VECTOR_CFP
    # a three-pair merge_content_key from the same run
    merge = [r for r in _fixture_rows() if r["proposal_id"] == "irp_2d6e0a1a3555669f7f629c5b19d76634"]
    assert len(merge) == 3 and {r["kind"] for r in merge} == {"merge_content_key"}
    assert content_fingerprint(merge) == "cfp_9d33edf722ed505ed36acbbe"
    assert content_fingerprint(list(reversed(merge))) == "cfp_9d33edf722ed505ed36acbbe"   # row order free


def test_all_60_real_proposals_recompute_from_the_audit_rows_alone():
    """Every proposal of the real run, recomputed from ONLY the fields the review page shows, equals
    the committed fingerprints (also checked against an independent Node implementation of the spec
    in the module docstring: 60/60)."""
    rows = _fixture_rows()
    assert {k for r in rows for k in r} == {"proposal_id", "kind", "tier", "listing", "anchor"}
    want = json.loads(AUDIT_FINGERPRINTS.read_text())
    got = content_fingerprints_from_audit_rows(rows)
    assert len(got) == 60 and got == want
    assert all(CONTENT_FINGERPRINT_RE.fullmatch(v) for v in got.values())


def _tricky_side(pk, title, price, size, url="https://x.example/p", cur="USD"):
    return {"product_key": pk, "canonical_url": url, "title": title, "size": size, "price": price, "currency": cur}


TRICKY_ROWS = [
    {"proposal_id": "irp_" + "1" * 32, "kind": "merge_content_key", "tier": "T0b",
     "listing": _tricky_side("ext:retailer:b", 'Tab\tNL\nQuote" back\\\\ \x01 \x7f   é 한국 😀', 10.125,
                             {"value": 1.45, "unit": "ml", "pack": 2}),
     "anchor": _tricky_side("ext:a", "Ünïcode", 1.005, {"value": 0.00005, "unit": "g", "pack": 1})},
    {"proposal_id": "irp_" + "1" * 32, "kind": "merge_content_key", "tier": "T0b",
     "listing": _tricky_side("ext:retailer:a", None, None, None, url=None, cur=None),
     "anchor": _tricky_side("ext:a", "Ünïcode", 2.675, {"value": 150, "unit": "ml", "pack": 1})},
    {"proposal_id": "irp_" + "2" * 32, "kind": "attach_membership", "tier": "T0a",
     "listing": _tricky_side("ext:retailer:c", "x", 0.125, {"value": 9.99995, "unit": "oz", "pack": 1}),
     "anchor": _tricky_side("ext:z", "y", 123456.785, {"value": 1e-7, "unit": "oz", "pack": 1})},
    # two anchors: listing order (a, b) and anchor order (y, z) disagree -- the sort is listing first
    {"proposal_id": "irp_" + "3" * 32, "kind": "merge_content_key", "tier": "T0c",
     "listing": _tricky_side("ext:retailer:a", "p", 5, {"value": 30, "unit": "ml", "pack": 1}),
     "anchor": _tricky_side("ext:z", "q", 5, {"value": 30, "unit": "ml", "pack": 1})},
    {"proposal_id": "irp_" + "3" * 32, "kind": "merge_content_key", "tier": "T0c",
     "listing": _tricky_side("ext:retailer:b", "p", 5, {"value": 30, "unit": "ml", "pack": 1}),
     "anchor": _tricky_side("ext:y", "q", 5, {"value": 30, "unit": "ml", "pack": 1})},
]


def test_adversarial_vectors_match_javascript():
    """Rounding ties, binary-inexact decimals, control characters, U+2028, astral characters and nulls.
    The expected values were produced by Node running the docstring's JS reference."""
    assert content_fingerprints_from_audit_rows(TRICKY_ROWS) == {
        "irp_" + "1" * 32: "cfp_5dca04d93209d5be30b2c1a2",
        "irp_" + "2" * 32: "cfp_09dfc09e358780da07076c64",
        "irp_" + "3" * 32: "cfp_743c841b932b5f8958f5cd1b",
    }


@pytest.mark.parametrize("x,places,want", [
    (1.005, 2, "1.00"), (2.675, 2, "2.67"), (10.125, 2, "10.13"), (0.125, 2, "0.13"), (1.45, 1, "1.4"),
    (67, 2, "67.00"), (67.0, 2, "67.00"), (9.5, 4, "9.5000"), (150, 4, "150.0000"), (-0.0, 2, "0.00"),
    (-0.001, 2, "-0.00"), (1e-7, 4, "0.0000"), (0.00005, 4, "0.0001"), ("10.81", 2, "10.81"), (None, 2, None),
])
def test_js_fixed_is_javascripts_toFixed(x, places, want):  # values checked against Node's toFixed
    assert js_fixed(x, places) == want


@pytest.mark.parametrize("x", [float("nan"), float("inf"), -float("inf"), 1e21, True])
def test_js_fixed_refuses_what_toFixed_would_not_print_plainly(x):
    with pytest.raises(ValueError):
        js_fixed(x, 2)


def test_integral_floats_and_ints_fingerprint_the_same():
    """Cloud Logging re-renders 67.0 as 67 and 150.0 as 150: the page sees ints, the planner floats."""
    row = json.loads(json.dumps(TRICKY_ROWS[2]))
    as_float = json.loads(json.dumps(row))
    as_float["anchor"]["size"] = {"value": 150.0, "unit": "ml", "pack": 1.0}
    as_float["anchor"]["price"] = 67.0
    as_int = json.loads(json.dumps(as_float).replace("150.0", "150").replace("67.0", "67").replace("1.0}", "1}"))
    assert as_int["anchor"]["price"] == 67 and isinstance(as_int["anchor"]["price"], int)
    assert isinstance(as_int["anchor"]["size"]["pack"], int)
    assert content_fingerprint([as_float]) == content_fingerprint([as_int])


@pytest.mark.parametrize("path,new", [
    (("listing", "product_key"), "ext:retailer:other"), (("anchor", "product_key"), "ext:other"),
    (("listing", "canonical_url"), "https://x.example/q"), (("anchor", "canonical_url"), None),
    (("listing", "title"), "x "), (("anchor", "title"), "Y"),
    (("listing", "size", "value"), 9.9999), (("anchor", "size", "unit"), "ml"),
    (("listing", "size", "pack"), 2), (("anchor", "size"), None),
    (("listing", "price"), 0.12), (("anchor", "price"), None),
    (("listing", "currency"), "SGD"), (("anchor", "currency"), "usd"),
    (("tier",), "T0b"), (("kind",), "merge_content_key"),
])
def test_every_reviewed_field_moves_the_fingerprint(path, new):
    row = json.loads(json.dumps(TRICKY_ROWS[2]))
    base = content_fingerprint([row])
    target = row
    for k in path[:-1]:
        target = target[k]
    target[path[-1]] = new
    assert content_fingerprint([row]) != base


def test_a_pair_added_or_removed_moves_the_fingerprint():
    merge = [r for r in _fixture_rows() if r["proposal_id"] == "irp_2d6e0a1a3555669f7f629c5b19d76634"]
    assert content_fingerprint(merge[:2]) != content_fingerprint(merge)
    extra = json.loads(json.dumps(merge[0]))
    extra["listing"]["product_key"] = "ext:retailer:" + "9" * 32
    assert content_fingerprint([*merge, extra]) != content_fingerprint(merge)


@pytest.mark.parametrize("mutate", [
    lambda r: r["listing"].__setitem__("title", 5),
    lambda r: r["listing"].__setitem__("product_key", None),
    lambda r: r.__setitem__("tier", None),
    lambda r: r["anchor"]["size"].__setitem__("unit", 3),
    lambda r: r["anchor"].__setitem__("currency", ["USD"]),
])
def test_a_non_string_field_refuses(mutate):
    row = json.loads(json.dumps(TRICKY_ROWS[2]))
    mutate(row)
    with pytest.raises(ValueError):
        content_fingerprint([row])


def test_an_empty_proposal_or_a_repeated_pair_refuses():
    with pytest.raises(ValueError):
        content_fingerprint([])
    row = TRICKY_ROWS[2]
    with pytest.raises(ValueError, match="twice"):
        content_fingerprint([row, json.loads(json.dumps(row))])


def test_the_fingerprint_ignores_rule_version_and_vetoes_checked(monkeypatch):
    """#2645 may bump the rule version or the veto list: the labels stay valid (the write's cmd_
    digest still changes, so a fresh dry run re-mints it)."""
    plan = _mixed().plan()
    monkeypatch.setattr(cmm, "RULE_VERSION", plan.proposals[0]["evidence"]["rule_version"] + "-next")
    monkeypatch.setattr(cmm, "VETOES_CHECKED", (*cmm.VETOES_CHECKED, "a_new_veto"))
    bumped = _mixed().plan()
    assert _ids(bumped) == _ids(plan)
    assert bumped.proposals[0]["evidence"]["rule_version"] != plan.proposals[0]["evidence"]["rule_version"]
    assert bumped.digest() != plan.digest()
    assert bumped.content_fingerprints() == plan.content_fingerprints()


def test_a_proposal_whose_pairs_do_not_name_every_moved_listing_refuses():
    plan = _mixed().plan()
    merge = next(p for p in plan.proposals if p["kind"] == "merge_content_key")
    drop = next(i for i, pr in enumerate(plan.pairs) if pr["proposal_id"] == merge["proposal_id"])
    del plan.pairs[drop]
    with pytest.raises(SelectionError, match="moves"):
        plan.content_fingerprints()


# --- the selection's refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["only", "exclude"])
def test_an_id_not_among_the_runs_proposals_is_refused_by_name(mode):
    plan = _mixed().plan()
    stale = "irp_" + "0" * 32
    with pytest.raises(SelectionError, match=stale):
        plan.select(mode, {**_entries(plan, _ids(plan)[:1]), stale: "cfp_" + "0" * 24})
    assert plan.selection is None and len(plan.proposals) == 6   # nothing narrowed


@pytest.mark.parametrize("mode", ["only", "exclude"])
def test_an_empty_selection_is_refused(mode):
    plan = _mixed().plan()
    with pytest.raises(SelectionError, match="names no proposal_id"):
        plan.select(mode, {})


def test_excluding_every_proposal_is_refused():
    plan = _mixed().plan()
    with pytest.raises(SelectionError, match="keeps no proposal"):
        plan.select("exclude", _entries(plan, _ids(plan)))


def test_a_plan_is_selected_at_most_once():
    plan = _mixed().plan()
    plan.select("only", _entries(plan, _ids(plan)[:3]))
    with pytest.raises(SelectionError, match="already"):
        plan.select("only", _entries(plan, _ids(plan)[:2]))


@pytest.mark.parametrize("mode", ["only", "exclude"])
def test_a_fingerprint_that_is_not_the_reviewed_content_refuses(mode):
    plan = _mixed().plan()
    ent = _entries(plan, _ids(plan)[:3])
    victim = _ids(plan)[1]
    ent[victim] = "cfp_" + "0" * 24
    with pytest.raises(SelectionError, match=f"fingerprint mismatch.*{victim}"):
        plan.select(mode, ent)
    assert plan.selection is None and len(plan.proposals) == 6


@pytest.mark.parametrize("change", ["price", "title", "canonical_url"])
def test_a_selected_id_whose_content_changed_since_review_refuses(change):
    """The controller's repro: the id pins only the structural move, so the same id comes back with a
    new price or title -- the reviewed fingerprint no longer matches."""
    cat = _mixed()
    reviewed = cat.plan()
    ids = _ids(reviewed)
    ent = _entries(reviewed, ids[:3])
    if change == "price":
        cat.offers[1]["price"] = 25
    elif change == "title":
        cat.products[1]["title"] = "Laneige Lip Sleeping Mask 20g"      # still the same match
    else:
        cat.products[1]["canonical_url"] = "https://moved.example/p/1"
    now = cat.plan()
    assert _ids(now) == ids                                          # same ids
    with pytest.raises(SelectionError, match="fingerprint mismatch"):
        now.select("only", ent)


# --- what the selected run reports -------------------------------------------------------------------------


def test_counts_samples_and_the_audit_sample_are_of_the_subset_and_say_so():
    full = _mixed().plan()
    ids = _ids(full)
    plan = _selected("only", ids[:2])
    summary = plan.summary()
    head = summary[0]
    assert head["proposals"] == 2 and sum(head["by_tier"].values()) == 2
    assert head["listings_moving"] == len(plan.pairs) < len(full.pairs)
    sel = head["selection"]
    assert sel["applied"] is True and sel["mode"] == "only" and "run_digest" not in sel
    assert (sel["selected"], sel["of"], sel["ids_given"], sel["fingerprints_verified"]) == (2, 6, 2, 2)
    assert head["digest"] == plan.digest() != full.digest()
    brief = {"mode": "only", "selected": 2, "of": 6}
    samples = [line for line in summary if line["section"] == "samples"]
    assert samples and all(line["selection"] == brief for line in samples)
    assert {p["proposal_id"] for line in samples for p in line["proposals"]} <= set(ids[:2])
    fps = next(line for line in summary if line["section"] == "fingerprints")
    assert fps["selection"] == brief and fps["content_fingerprints"] == _entries(full, ids[:2])
    audit = plan.audit_rows()
    assert audit and {r["proposal_id"] for r in audit} <= set(ids[:2])
    assert all(r["selection"] == brief for r in audit)
    assert "selection" not in full.summary()[0] and all("selection" not in r for r in full.audit_rows())


def test_every_audit_row_sample_and_the_fingerprints_line_carry_the_fingerprint():
    plan = _mixed().plan()
    cfps = plan.content_fingerprints()
    assert sorted(cfps) == _ids(plan)
    rows = plan.audit_rows(size=2)                         # a sample still carries the FULL proposal's cfp
    assert all(r["content_fingerprint"] == cfps[r["proposal_id"]] for r in rows)
    summary = plan.summary()
    assert all(p["content_fingerprint"] == cfps[p["proposal_id"]]
               for line in summary if line["section"] == "samples" for p in line["proposals"])
    assert next(line for line in summary if line["section"] == "fingerprints")["content_fingerprints"] == cfps


def _two_anchors():
    cat = _mixed()
    brand_store(cat, "ext:laneige-water-sleeping-mask::w1", "LANEIGE", "Water Sleeping Mask 70ml",
                host="us.laneige.com")
    retailer(cat, 30, "Laneige", "Water Sleeping Mask 70ml", host="r30.example")
    return cat


def test_new_multi_merchant_groups_are_recounted_for_the_subset():
    cat = _two_anchors()
    full = cat.plan()
    assert full.new_multi_merchant_groups == 2   # each brand store gains a retailer host
    lip = [p["proposal_id"] for p in full.proposals if "lip" in p["keeper_product_key"]]
    assert 0 < len(lip) < len(full.proposals)
    plan = _selected("only", lip, cat)
    assert plan.new_multi_merchant_groups == 1
    assert plan.summary()[0]["expected_new_multi_merchant_groups"] == 1


# --- the ops script ---------------------------------------------------------------------------------------


def _script():
    from scripts.ops import propose_cross_merchant_matches as script
    return script


A, B, C = ("irp_" + c * 32 for c in "abc")
FA, FB, FC = ("cfp_" + c * 24 for c in "def")
EA, EB, EC = f"{A}={FA}", f"{B}={FB}", f"{C}={FC}"
SELECTION_FLAGS = ("--only-proposals", "--exclude-proposals", "--only-proposal-ids", "--exclude-proposal-ids")


def _args(*argv):
    return _script().parse_args(["--brands", "laneige", *argv])


def test_the_four_selection_flags_are_mutually_exclusive(tmp_path):
    f = tmp_path / "ids.txt"
    f.write_text(EA + "\n")
    value = {"--only-proposals": str(f), "--exclude-proposals": str(f),
             "--only-proposal-ids": EA, "--exclude-proposal-ids": EA}
    for i, x in enumerate(SELECTION_FLAGS):
        for y in SELECTION_FLAGS[i + 1:]:
            with pytest.raises(SystemExit):
                _args(x, value[x], y, value[y])
        with pytest.raises(SystemExit):
            _args("--explain", x, value[x])
    args = _args("--only-proposal-ids", EA)
    assert args.only_proposal_ids == EA and args.only_proposals is None and args.exclude_proposal_ids is None


@pytest.mark.parametrize("flag", ["--exclude-proposals", "--exclude-proposal-ids"])
def test_exclude_is_refused_with_write_proposals_but_allowed_for_a_dry_run(tmp_path, flag):
    f = tmp_path / "ids.txt"
    f.write_text(EA + "\n")
    value = str(f) if flag == "--exclude-proposals" else EA
    with pytest.raises(SystemExit):
        _args("--write-proposals", "--expect-digest", "cmd_x", "--max-proposals", "5", flag, value)
    assert getattr(_args(flag, value), flag[2:].replace("-", "_")) == value
    _args("--write-proposals", "--expect-digest", "cmd_x", "--max-proposals", "5", "--only-proposal-ids", EA)


def test_one_normalisation_strips_dedupes_and_sorts():
    assert normalise_proposal_ids([f" {EB} ", EA, EB, f"{EC}\t"]) == {A: FA, B: FB, C: FC}
    assert list(normalise_proposal_ids([EC, EA])) == [A, C]
    assert normalise_proposal_ids([]) == {}


@pytest.mark.parametrize("bad,reason", [
    ([EA, ""], "whitespace-only"), ([EA, "   "], "whitespace-only"),
    ([A], "is not <proposal_id>=<cfp_"), ([f"{A}:{FA}"], "is not <proposal_id>=<cfp_"),
    ([f"irq_{'a' * 32}={FA}"], "not a proposal_id"), ([f"irp_{'a' * 31}={FA}"], "not a proposal_id"),
    ([f"irp_{'a' * 33}={FA}"], "not a proposal_id"), ([f"irp_{'A' * 32}={FA}"], "not a proposal_id"),
    ([f"{A}=cfp_{'d' * 23}"], "not a content fingerprint"), ([f"{A}=cfp_{'d' * 25}"], "not a content fingerprint"),
    ([f"{A}=cfp_{'D' * 24}"], "not a content fingerprint"), ([f"{A}=cmd_{'d' * 24}"], "not a content fingerprint"),
    ([f"{A} = {FA}"], "not a proposal_id"), ([f"{A}={FA}=x"], "not a content fingerprint"),
    ([EA, f"{A}={FB}"], "two content fingerprints"),
    ([EA, 3], "not a string"), ([None], "not a string"),
    (["﻿" + EA], "BOM"),
])
def test_a_malformed_entry_refuses(bad, reason):
    with pytest.raises(SelectionError, match=reason):
        normalise_proposal_ids(bad)


def test_parse_proposal_ids_reads_lines_or_a_json_list_through_the_one_normalisation():
    script = _script()
    assert script.parse_proposal_ids(f"{EB}\n\n# labelled clean\n  {EA}  \n{EB}\n") == {A: FA, B: FB}
    assert script.parse_proposal_ids(json.dumps([EB, EA, EB])) == {A: FA, B: FB}
    assert script.parse_proposal_ids("\n# nothing\n") == {}
    for bad in (json.dumps([EA, 3]), json.dumps([EA, ""]), json.dumps([EA, "  "]), "[" + EA,
                json.dumps({"ids": [EA]}), f"{EA}\nirp_short=cfp_x\n", f"{EA}\n{A}\n"):
        with pytest.raises(script.Refused):
            script.parse_proposal_ids(bad)


def test_load_selection_normalises_both_forms_identically_and_shell_quotes_the_path(tmp_path):
    script = _script()
    d = tmp_path / "Peng's labels"
    d.mkdir()
    f = d / "clean 42.json"
    f.write_text(json.dumps([EB, EA]))
    sel = script.load_selection(_args("--exclude-proposals", str(f)))
    assert sel[:2] == ("exclude", {A: FA, B: FB})
    import shlex
    assert sel[2] == f"--exclude-proposals {shlex.quote(str(f))}" and shlex.split(sel[2])[1] == str(f)
    inline = script.load_selection(_args("--exclude-proposal-ids", f" {EB},{EA} ,{EB}"))
    assert inline == ("exclude", {A: FA, B: FB}, f"--exclude-proposal-ids {EA},{EB}")
    assert inline[:2] == sel[:2]
    assert script.load_selection(_args()) is None


@pytest.mark.parametrize("flag,value,reason", [
    ("--only-proposals", "gs://bkt/cm/clean.txt", "gs:// is not read"),
    ("--exclude-proposals", "GS://bkt/x", "gs:// is not read"),
    ("--only-proposals", "/nonexistent/m4sel/ids.txt", "could not be read: FileNotFoundError"),
    ("--only-proposal-ids", "", "empty selection"),
    ("--only-proposal-ids", "   ", "empty selection"),
    ("--exclude-proposal-ids", ",", "whitespace-only"),
    ("--only-proposal-ids", f"{EA}, ,{EB}", "whitespace-only"),
    ("--only-proposal-ids", f"{EA},", "whitespace-only"),
    ("--only-proposal-ids", A, "is not <proposal_id>=<cfp_"),
    ("--only-proposal-ids", f"irq_{'a' * 32}={FA}", "not a proposal_id"),
    ("--only-proposal-ids", f"{EA},irp_{'b' * 31}={FB}", "not a proposal_id"),
    ("--exclude-proposal-ids", f"irp_{'c' * 40}={FC}", "not a proposal_id"),
])
def test_a_bad_selection_refuses(flag, value, reason):
    script = _script()
    with pytest.raises(script.Refused, match=reason):
        script.load_selection(_args(flag, value))


def test_an_empty_malformed_undecodable_or_bom_file_refuses(tmp_path):
    script = _script()
    for body, reason in ((b"\n\n", "empty selection"), (b"[]", "empty selection"),
                         (f"{EA}\nnot-an-id\n".encode(), "is not <proposal_id>"),
                         (json.dumps([EA, " "]).encode(), "whitespace-only"),
                         (b"\xff\xfe" + EA.encode("utf-16-le"), "could not be read: UnicodeDecodeError"),
                         (("﻿" + EA + "\n").encode("utf-8"), "BOM")):
        f = tmp_path / "ids.txt"
        f.write_bytes(body)
        with pytest.raises(script.Refused, match=reason):
            script.load_selection(_args("--only-proposals", str(f)))
    with pytest.raises(script.Refused, match="could not be read: IsADirectoryError"):
        script.load_selection(_args("--only-proposals", str(tmp_path)))


def test_any_unexpected_failure_while_loading_refuses_never_a_traceback(monkeypatch):
    script = _script()

    def boom(*_a, **_kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(script, "normalise_proposal_ids", boom)
    with pytest.raises(script.Refused, match="RuntimeError: unexpected"):
        script.load_selection(_args("--only-proposal-ids", EA))


class _NoConn:
    def transaction(self, **_kw):
        raise AssertionError("nothing may be read or written")


@pytest.fixture()
def offline(monkeypatch):
    """run() over the _mixed() catalog instead of a database read."""
    script = _script()
    cat = _mixed()

    async def fake_export(conn, market, *, brands=(), limit_groups=None):
        return cat.snapshot(), {"products": len(cat.products)}

    monkeypatch.setattr(script, "export_snapshot", fake_export)
    return script, cat


def _lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def _run(script, *argv, conn=None):
    return asyncio.run(script.run(conn or _NoConn(), _args(*argv)))


def _write_entries(path, plan, ids):
    path.write_text("\n".join(f"{i}={fp}" for i, fp in _entries(plan, ids).items()) + "\n")


def test_the_dry_run_under_a_selection_prints_the_subsets_digest_count_and_next_step(offline, tmp_path, capsys):
    script, cat = offline
    full = cat.plan()
    keep = _ids(full)[1:4]
    d = tmp_path / "my labels"
    d.mkdir()
    f = d / "clean.txt"
    _write_entries(f, full, keep)
    out_dir = tmp_path / "out"
    assert _run(script, "--out", str(out_dir), "--only-proposals", str(f)) == 0
    lines = _lines(capsys)
    outputs = next(line for line in lines if line["section"] == "outputs")
    sub = _selected("only", keep, cat)
    assert outputs["digest"] == sub.digest() != full.digest()
    assert outputs["proposals"] == 3 and outputs["selection"]["selected"] == 3
    assert outputs["next_step"].startswith(
        f"--write-proposals --expect-digest {sub.digest()} --max-proposals 3 --only-proposals '{f}' ")
    head = next(line for line in lines if line["section"] == "proposals")
    assert head["digest"] == sub.digest() and head["selection"]["mode"] == "only"
    audit = [line for line in lines if line["section"] == "audit"]
    assert audit and all(a["selection"]["mode"] == "only" for a in audit)
    written = [json.loads(x)["proposal_id"] for x in (out_dir / "proposals.jsonl").read_text().splitlines()]
    assert written == keep                         # exactly what --write-proposals would insert


def test_the_plan_path_and_the_audit_rows_alone_agree_on_every_fingerprint(offline, tmp_path, capsys):
    """What the page computes from the printed audit lines (after Cloud Logging turns 24.0 into 24)
    equals what the writer recomputes from the plan, for every proposal."""
    script, cat = offline
    assert _run(script, "--out", str(tmp_path / "o")) == 0
    lines = _lines(capsys)

    def like_cloud_logging(v):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, dict):
            return {k: like_cloud_logging(x) for k, x in v.items()}
        if isinstance(v, list):
            return [like_cloud_logging(x) for x in v]
        return v

    audit = [like_cloud_logging(line) for line in lines if line["section"] == "audit"]
    assert any(isinstance(a["listing"]["price"], int) for a in audit)        # the conversion bit
    page = content_fingerprints_from_audit_rows(audit)
    printed = next(line for line in lines if line["section"] == "fingerprints")["content_fingerprints"]
    assert page == printed == cat.plan().content_fingerprints() and len(page) == 6
    assert all(a["content_fingerprint"] == page[a["proposal_id"]] for a in audit)


@pytest.mark.parametrize("case", ["full_digest_with_subset_flag", "subset_digest_without_flag",
                                  "subset_digest_with_other_subset"])
def test_write_proposals_refuses_a_digest_minted_for_another_selection(offline, tmp_path, capsys, case):
    script, cat = offline
    full = cat.plan()
    ids = _ids(full)
    keep, other = ids[:4], ids[1:5]
    sub = _selected("only", keep, cat)
    digest, flag = {
        "full_digest_with_subset_flag": (full.digest(), ["--only-proposal-ids", _tok(full, keep)]),
        "subset_digest_without_flag": (sub.digest(), []),
        "subset_digest_with_other_subset": (sub.digest(), ["--only-proposal-ids", _tok(full, other)]),
    }[case]
    assert _run(script, "--out", str(tmp_path / "o"), "--write-proposals", "--expect-digest", digest,
                "--max-proposals", "60", *flag) == 2
    refused = [line for line in _lines(capsys) if line["section"] == "refused"]
    assert len(refused) == 1 and "proposal set changed" in refused[0]["reason"]


def test_a_stale_id_or_an_empty_file_refuses_the_run_with_exit_2(offline, tmp_path, capsys, monkeypatch):
    script, cat = offline
    stale = "irp_" + "f" * 32
    f = tmp_path / "stale.txt"
    f.write_text(f"{_tok(cat.plan(), _ids(cat.plan())[:1])}\n{stale}=cfp_{'0' * 24}\n")
    for mode in ("--only-proposals", "--exclude-proposals"):
        assert _run(script, "--out", str(tmp_path / "o"), mode, str(f)) == 2
        refused = [line for line in _lines(capsys) if line["section"] == "refused"]
        assert len(refused) == 1 and stale in refused[0]["reason"] and refused[0]["selection"] == f"{mode} {f}"
    assert not (tmp_path / "o").exists()           # nothing written for a refused selection

    async def must_not_read(*_a, **_kw):
        raise AssertionError("an empty selection is refused before the database is read")

    monkeypatch.setattr(script, "export_snapshot", must_not_read)
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing labelled\n")
    assert _run(script, "--out", str(tmp_path / "o"), "--only-proposals", str(empty)) == 2
    assert "empty selection" in [line for line in _lines(capsys) if line["section"] == "refused"][0]["reason"]


def _outputs(capsys):
    return next(line for line in _lines(capsys) if line["section"] == "outputs")


@pytest.mark.parametrize("mode", ["only", "exclude"])
def test_the_file_and_inline_forms_of_a_mode_give_the_same_digest(offline, tmp_path, capsys, mode):
    script, cat = offline
    plan = cat.plan()
    chosen = _ids(plan)[1:4]
    ent = _entries(plan, chosen)
    tok = [f"{i}={ent[i]}" for i in chosen]
    f = tmp_path / "clean.txt"
    f.write_text("\n".join(reversed(tok)) + "\n" + tok[0] + "\n")      # unsorted, duplicated
    assert _run(script, "--out", str(tmp_path / "a"), f"--{mode}-proposals", str(f)) == 0
    by_file = _outputs(capsys)
    inline = ",".join([tok[2], f" {tok[0]} ", tok[1], tok[0]])
    assert _run(script, "--out", str(tmp_path / "b"), f"--{mode}-proposal-ids", inline) == 0
    by_ids = _outputs(capsys)
    assert by_file["digest"] == by_ids["digest"] and by_file["proposals"] == by_ids["proposals"]
    assert by_file["next_step"].startswith(
        f"--write-proposals --expect-digest {by_file['digest']} --max-proposals {by_file['proposals']}"
        f" --{mode}-proposals {f} ")
    assert by_ids["next_step"].startswith(
        f"--write-proposals --expect-digest {by_ids['digest']} --max-proposals {by_ids['proposals']}"
        f" --{mode}-proposal-ids {','.join(sorted(tok))} ")
    other = "exclude" if mode == "only" else "only"
    assert _run(script, "--out", str(tmp_path / "c"), f"--{other}-proposal-ids", ",".join(tok)) == 0
    assert _outputs(capsys)["digest"] != by_ids["digest"]                 # the mode is folded in


def test_a_write_refuses_when_a_selected_proposals_content_changed_since_review(offline, tmp_path, capsys):
    script, cat = offline
    plan = cat.plan()
    chosen = _ids(plan)[:3]
    tok = _tok(plan, chosen)
    assert _run(script, "--out", str(tmp_path / "a"), "--only-proposal-ids", tok) == 0
    digest = _outputs(capsys)["digest"]
    cat.offers[1]["price"] = 25                     # same ids, a selected pair's price changed
    assert _ids(cat.plan())[:3] == chosen
    for argv in (["--write-proposals", "--expect-digest", digest, "--max-proposals", "3"], []):
        assert _run(script, "--out", str(tmp_path / "b"), *argv, "--only-proposal-ids", tok) == 2
        refused = next(line for line in _lines(capsys) if line["section"] == "refused")
        assert "fingerprint mismatch" in refused["reason"]


@pytest.mark.parametrize("flag,value", [
    ("--only-proposals", "gs://bkt/cm/clean.txt"),
    ("--only-proposal-ids", " "),
    ("--only-proposal-ids", f"irp_123={FA}"),
    ("--only-proposal-ids", EA + ",,"),
    ("--only-proposal-ids", A),
    ("--exclude-proposal-ids", "irp_123"),
])
def test_a_bad_selection_exits_2_before_the_database_is_read(offline, tmp_path, capsys, monkeypatch, flag, value):
    script, _cat = offline

    async def must_not_read(*_a, **_kw):
        raise AssertionError("a bad selection is refused before the database is read")

    monkeypatch.setattr(script, "export_snapshot", must_not_read)
    modes = [[]] if flag.startswith("--exclude") else [[], ["--write-proposals", "--expect-digest", "cmd_x",
                                                            "--max-proposals", "5"]]
    for mode in modes:
        assert _run(script, "--out", str(tmp_path / "o"), *mode, flag, value) == 2
        refused = [line for line in _lines(capsys) if line["section"] == "refused"]
        assert len(refused) == 1
    assert not (tmp_path / "o").exists()


# --- the selected write's guards (fake connection; the PG test runs them for real) --------------------------


class _FakeConn:
    def __init__(self, pending=(), existing=()):
        self.pending, self.existing, self.fetched = list(pending), list(existing), []

    @asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self, **_kw):
        return self._tx()

    async def fetch(self, sql, *args):
        script = _script()
        self.fetched.append(sql)
        if sql == script.PENDING_SQL:
            assert args == (cmm.STRATEGY,)
            return self.pending
        if sql == script.EXISTING_SQL:
            return [r for r in self.existing if r["proposal_id"] in args[0]]
        raise AssertionError(sql)


@pytest.fixture()
def upserts(monkeypatch):
    calls = []

    async def fake_upsert(conn, proposals, *, module_writer):
        calls.append([p["proposal_id"] for p in proposals])
        return {"inserted": 1}

    import services.identity_resolution as ir
    monkeypatch.setattr(ir, "upsert_proposals", fake_upsert)
    return calls


def _write(plan, conn):
    return asyncio.run(_script().write_proposals(conn, plan, expect_digest=plan.digest(), max_proposals=100,
                                                 run_id="cmss_test"))


def test_a_selected_write_with_a_pending_run_of_the_strategy_refuses(upserts):
    script = _script()
    plan = _selected("only", _ids(_mixed().plan())[:2])
    conn = _FakeConn(pending=[{"proposal_id": "irp_" + "e" * 32, "proposal_run_id": "cmss_earlier"}])
    with pytest.raises(script.Refused, match="pending.*cmss_earlier"):
        _write(plan, conn)
    assert upserts == []


def test_a_selected_write_that_would_dedupe_refuses(upserts):
    script = _script()
    ids = _ids(_mixed().plan())
    plan = _selected("only", ids[:2])
    conn = _FakeConn(existing=[{"proposal_id": ids[1], "status": "rejected", "proposal_run_id": "cmss_old"}])
    with pytest.raises(script.Refused, match="would dedupe"):
        _write(plan, conn)
    assert upserts == []


def test_a_selected_write_that_deduped_anyway_is_rolled_back(monkeypatch):
    """A concurrent writer between the check and the insert: the post-insert count refuses."""
    script = _script()
    import services.identity_resolution as ir

    async def deduping_upsert(conn, proposals, *, module_writer):
        return {"inserted": 0}

    monkeypatch.setattr(ir, "upsert_proposals", deduping_upsert)
    plan = _selected("only", _ids(_mixed().plan())[:2])
    with pytest.raises(script.Refused, match="deduped onto existing rows"):
        _write(plan, _FakeConn())


def test_a_clean_selected_write_inserts_exactly_the_selection(upserts):
    ids = _ids(_mixed().plan())
    plan = _selected("only", ids[:2])
    conn = _FakeConn(existing=[{"proposal_id": ids[3], "status": "rejected", "proposal_run_id": "cmss_old"}])
    result = _write(plan, conn)
    assert upserts == [[ids[0]], [ids[1]]] and result["inserted"] == 2 and result["deduped"] == 0
    assert result["selection"]["mode"] == "only"
    script = _script()
    assert conn.fetched == [script.PENDING_SQL, script.EXISTING_SQL]


def test_an_unselected_write_keeps_its_dedupe_behaviour(upserts):
    """The guards are for SELECTED writes; a full-set write still dedupes onto existing rows."""
    plan = _mixed().plan()
    conn = _FakeConn(pending=[{"proposal_id": "irp_" + "e" * 32, "proposal_run_id": "cmss_earlier"}])
    assert _write(plan, conn)["inserted"] == 6 and conn.fetched == []


def test_write_proposals_itself_refuses_an_exclude_selection(upserts):
    script = _script()
    plan = _selected("exclude", _ids(_mixed().plan())[4:])
    with pytest.raises(script.Refused, match="only an --only selection"):
        _write(plan, _FakeConn())
    assert upserts == []
