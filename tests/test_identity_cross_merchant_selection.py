"""cross_merchant_same_size (M4): --only-proposals / --exclude-proposals. Pure -- no database.

A hand-labelled subset of a dry run's proposals is the only thing a --write-proposals run may insert,
and the selection is folded into the digest: a subset's digest never authorises the full set, another
subset, or the same subset reached the other way round; the full set's never authorises a subset."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.identity_cross_merchant_match import (  # noqa: E402
    PROPOSAL_ID_RE,
    SelectionError,
    Snapshot,
    normalise_proposal_ids,
    plan_market,
    proposal_set_digest,
    select_proposals,
)
from test_identity_cross_merchant_match import _mixed, brand_store, retailer  # noqa: E402


def _ids(plan):
    return [p["proposal_id"] for p in plan.proposals]


def _selected(mode, ids):
    plan = _mixed().plan()
    plan.select(mode, ids)
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
    # the same four proposals reached with --exclude are a different authorisation
    excl = _selected("exclude", ids[4:])
    assert _ids(excl) == a and excl.digest() != d_a
    # the selection marker itself is folded in: the bare digest of the same proposals is not d_a
    assert proposal_set_digest(_selected("only", a).proposals) != d_a
    # an unselected plan's digest is unchanged by this feature (a printed full digest still holds)
    assert d_full == proposal_set_digest(full.proposals)


def test_proposal_ids_are_deterministic_across_runs_over_the_same_inputs():
    """proposal_id = irp_ + sha256(proposal_key)[:32]; the key is strategy, merchant, target content_key
    and the move's member fingerprint -- no clock, no run id, no input order."""
    one, two = _mixed().plan(), _mixed().plan()          # two independently built catalogs
    assert _ids(one) == _ids(two) and one.digest() == two.digest()
    assert [p["proposal_key"] for p in one.proposals] == [p["proposal_key"] for p in two.proposals]
    snap = _mixed().snapshot()
    shuffled = Snapshot(products=random.Random(5).sample(list(snap.products), len(snap.products)),
                        offers=list(reversed(snap.offers)), skus=snap.skus, ck_rows=snap.ck_rows,
                        memberships=snap.memberships, group_sizes=snap.group_sizes)
    assert _ids(plan_market(shuffled, "US")) == _ids(one)
    assert all(PROPOSAL_ID_RE.fullmatch(i) for i in _ids(one))


def test_the_subset_digest_binds_each_selected_id_to_its_content():
    """The id derives from the subjects (moved listing + anchor) and the move's keys, so a different
    anchor or listing is a different id; what CAN change under one id is the evidence (prices, sizes,
    titles, hosts, matched offers). proposal_set_digest's line per proposal carries
    proposal_fingerprint (kind, strategy, keeper, target content_key, subjects, evidence), so the
    subset digest moves with it."""
    cat = _mixed()
    plan = cat.plan()
    a = _ids(plan)[:3]
    before = {p["proposal_id"]: p for p in plan.proposals}
    plan.select("only", a)
    d_a = plan.digest()
    cat.offers[1]["price"] = 25        # one selected retailer's price: same pair, new evidence
    again = cat.plan()
    assert _ids(again) == list(before)                     # every id unchanged
    changed = [p["proposal_id"] for p in again.proposals if p != before[p["proposal_id"]]]
    assert len(changed) == 1 and changed[0] in a
    q, old = next(p for p in again.proposals if p["proposal_id"] == changed[0]), before[changed[0]]
    assert {k for k in q if q[k] != old[k]} == {"evidence"}
    again.select("only", a)
    assert _ids(again) == a and again.digest() != d_a
    # a selection over the unchanged proposals is unaffected
    rest = [i for i in a if i != changed[0]]
    p1, p2 = _mixed().plan(), cat.plan()
    p1.select("only", rest)
    p2.select("only", rest)
    assert p1.digest() == p2.digest()


def test_an_unknown_selection_mode_is_refused():
    plan = _mixed().plan()
    with pytest.raises(SelectionError):
        proposal_set_digest(plan.proposals, selection=("some", _ids(plan)))
    with pytest.raises(SelectionError):
        select_proposals(plan.proposals, "some", _ids(plan)[:1])


# --- the selection's refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["only", "exclude"])
def test_an_id_not_among_the_runs_proposals_is_refused_by_name(mode):
    plan = _mixed().plan()
    stale = "irp_" + "0" * 32
    with pytest.raises(SelectionError, match=stale):
        plan.select(mode, [_ids(plan)[0], stale])
    assert plan.selection is None and len(plan.proposals) == 6   # nothing narrowed


@pytest.mark.parametrize("mode", ["only", "exclude"])
def test_an_empty_selection_is_refused(mode):
    plan = _mixed().plan()
    with pytest.raises(SelectionError, match="names no proposal_id"):
        plan.select(mode, [])


def test_excluding_every_proposal_is_refused():
    plan = _mixed().plan()
    with pytest.raises(SelectionError, match="keeps no proposal"):
        plan.select("exclude", _ids(plan))


def test_a_plan_is_selected_at_most_once():
    plan = _mixed().plan()
    plan.select("only", _ids(plan)[:3])
    with pytest.raises(SelectionError, match="already"):
        plan.select("only", _ids(plan)[:2])


# --- what the selected run reports -------------------------------------------------------------------------


def test_counts_samples_and_the_audit_sample_are_of_the_subset_and_say_so():
    full = _mixed().plan()
    ids = _ids(full)
    plan = _selected("only", ids[:2])
    head = plan.summary()[0]
    assert head["proposals"] == 2 and sum(head["by_tier"].values()) == 2
    assert head["listings_moving"] == len(plan.pairs) < len(full.pairs)
    sel = head["selection"]
    assert sel["applied"] is True and sel["mode"] == "only"
    assert (sel["selected"], sel["of"], sel["ids_given"]) == (2, 6, 2)
    assert sel["run_digest"] == full.digest() and head["digest"] == plan.digest() != full.digest()
    samples = [line for line in plan.summary() if line["section"] == "samples"]
    assert samples and all(line["selection"] == {"mode": "only", "selected": 2, "of": 6} for line in samples)
    assert {p["proposal_id"] for line in samples for p in line["proposals"]} <= set(ids[:2])
    audit = plan.audit_rows()
    assert audit and {r["proposal_id"] for r in audit} <= set(ids[:2])
    assert all(r["selection"] == {"mode": "only", "selected": 2, "of": 6} for r in audit)
    # an unselected run says nothing about a selection
    assert "selection" not in full.summary()[0] and all("selection" not in r for r in full.audit_rows())


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
    plan = cat.plan()
    plan.select("only", lip)
    assert plan.new_multi_merchant_groups == 1
    assert plan.summary()[0]["expected_new_multi_merchant_groups"] == 1


def test_the_digest_folds_the_selected_ids_themselves():
    """Defence in depth: the marker carries the ids, so a selection whose ids disagree with the
    proposals it is paired with can never reproduce a digest."""
    plan = _mixed().plan()
    ids = _ids(plan)
    props = plan.proposals[:2]
    d = proposal_set_digest(props, selection=("only", ids[:2]))
    assert proposal_set_digest(props, selection=("only", list(reversed(ids[:2])))) == d
    assert proposal_set_digest(props, selection=("only", ids[:3])) != d
    assert proposal_set_digest(props, selection=("exclude", ids[:2])) != d


# --- the ops script ---------------------------------------------------------------------------------------


def _script():
    from scripts.ops import propose_cross_merchant_matches as script
    return script


A, B, C = ("irp_" + c * 32 for c in "abc")
SELECTION_FLAGS = ("--only-proposals", "--exclude-proposals", "--only-proposal-ids", "--exclude-proposal-ids")


def test_the_four_selection_flags_are_mutually_exclusive(tmp_path):
    script = _script()
    f = tmp_path / "ids.txt"
    f.write_text(A + "\n")
    value = {"--only-proposals": str(f), "--exclude-proposals": str(f),
             "--only-proposal-ids": A, "--exclude-proposal-ids": A}
    for i, x in enumerate(SELECTION_FLAGS):
        for y in SELECTION_FLAGS[i + 1:]:
            with pytest.raises(SystemExit):
                script.parse_args(["--brands", "laneige", x, value[x], y, value[y]])
        with pytest.raises(SystemExit):
            script.parse_args(["--brands", "laneige", "--explain", x, value[x]])
    args = script.parse_args(["--brands", "laneige", "--only-proposal-ids", A])
    assert args.only_proposal_ids == A and args.only_proposals is None and args.exclude_proposal_ids is None


def test_one_normalisation_strips_dedupes_and_sorts():
    assert normalise_proposal_ids([f" {B} ", A, B, f"{C}\t"]) == [A, B, C]
    assert normalise_proposal_ids([]) == []
    for bad in ([A, ""], [A, "   "], ["irq_" + "a" * 32], ["irp_" + "a" * 31], ["irp_" + "a" * 33],
                ["irp_" + "A" * 32], ["irp_" + "g" * 32], [A, 3], [None]):
        with pytest.raises(SelectionError):
            normalise_proposal_ids(bad)


def test_parse_proposal_ids_reads_lines_or_a_json_list_through_the_one_normalisation():
    script = _script()
    assert script.parse_proposal_ids(f"{B}\n\n# labelled clean\n  {A}  \n{B}\n") == [A, B]
    assert script.parse_proposal_ids(json.dumps([B, A, B])) == [A, B]
    assert script.parse_proposal_ids("\n# nothing\n") == []
    for bad in (json.dumps([A, 3]), json.dumps([A, ""]), json.dumps([A, "  "]), "[" + A,
                json.dumps({"ids": [A]}), f"{A}\nirp_short\n", f"{A}\n{A.upper()}\n"):
        with pytest.raises(script.Refused):
            script.parse_proposal_ids(bad)


def test_load_selection_normalises_both_forms_identically(tmp_path):
    script = _script()
    f = tmp_path / "ids.json"
    f.write_text(json.dumps([B, A]))
    sel = script.load_selection(script.parse_args(["--brands", "laneige", "--exclude-proposals", str(f)]))
    assert sel == ("exclude", [A, B], f"--exclude-proposals {f}")
    inline = script.load_selection(script.parse_args(["--brands", "laneige", "--exclude-proposal-ids",
                                                      f" {B},{A} ,{B}"]))
    assert inline == ("exclude", [A, B], f"--exclude-proposal-ids {A},{B}")
    assert inline[:2] == sel[:2]
    assert script.load_selection(script.parse_args(["--brands", "laneige"])) is None


@pytest.mark.parametrize("flag,value,reason", [
    ("--only-proposals", "gs://bkt/cm/clean.txt", "gs:// is not read"),
    ("--exclude-proposals", "GS://bkt/x", "gs:// is not read"),
    ("--only-proposals", "/nonexistent/m4sel/ids.txt", "cannot read"),
    ("--only-proposal-ids", "", "empty selection"),
    ("--only-proposal-ids", "   ", "empty selection"),
    ("--exclude-proposal-ids", ",", "whitespace-only"),
    ("--only-proposal-ids", f"{A}, ,{B}", "whitespace-only"),
    ("--only-proposal-ids", f"{A},", "whitespace-only"),
    ("--only-proposal-ids", "irq_" + "a" * 32, "not a proposal_id"),
    ("--only-proposal-ids", f"{A},irp_{'b' * 31}", "not a proposal_id"),
    ("--exclude-proposal-ids", f"irp_{'c' * 40}", "not a proposal_id"),
])
def test_a_bad_selection_refuses(flag, value, reason):
    script = _script()
    with pytest.raises(script.Refused, match=reason):
        script.load_selection(script.parse_args(["--brands", "laneige", flag, value]))


def test_an_empty_or_malformed_file_refuses(tmp_path):
    script = _script()
    for text, reason in (("\n\n", "empty selection"), ("[]", "empty selection"),
                         (f"{A}\nnot-an-id\n", "not a proposal_id"), (json.dumps([A, " "]), "whitespace-only")):
        f = tmp_path / "ids.txt"
        f.write_text(text)
        with pytest.raises(script.Refused, match=reason):
            script.load_selection(script.parse_args(["--brands", "laneige", "--only-proposals", str(f)]))


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


def _run(script, *argv):
    import asyncio
    return asyncio.run(script.run(_NoConn(), script.parse_args(["--brands", "laneige", *argv])))


def test_the_dry_run_under_a_selection_prints_the_subsets_digest_count_and_next_step(offline, tmp_path, capsys):
    script, cat = offline
    full = cat.plan()
    keep = _ids(full)[1:4]
    f = tmp_path / "clean.txt"
    f.write_text("\n".join(keep) + "\n")
    out_dir = tmp_path / "out"
    assert _run(script, "--out", str(out_dir), "--only-proposals", str(f)) == 0
    lines = _lines(capsys)
    outputs = next(line for line in lines if line["section"] == "outputs")
    sub = cat.plan()
    sub.select("only", keep)
    assert outputs["digest"] == sub.digest() != full.digest()
    assert outputs["proposals"] == 3 and outputs["selection"]["selected"] == 3
    assert outputs["selection"]["run_digest"] == full.digest()
    assert outputs["next_step"].startswith(
        f"--write-proposals --expect-digest {sub.digest()} --max-proposals 3 --only-proposals {f} ")
    head = next(line for line in lines if line["section"] == "proposals")
    assert head["digest"] == sub.digest() and head["selection"]["mode"] == "only"
    audit = [line for line in lines if line["section"] == "audit"]
    assert audit and all(a["selection"]["mode"] == "only" for a in audit)
    written = [json.loads(x)["proposal_id"] for x in (out_dir / "proposals.jsonl").read_text().splitlines()]
    assert written == keep                         # exactly what --write-proposals would insert


@pytest.mark.parametrize("case", ["full_digest_with_subset_flag", "subset_digest_without_flag",
                                  "subset_digest_with_other_subset", "subset_digest_via_exclude"])
def test_write_proposals_refuses_a_digest_minted_for_another_selection(offline, tmp_path, capsys, case):
    script, cat = offline
    full = cat.plan()
    ids = _ids(full)
    keep, other = ids[:4], ids[1:5]
    sub = cat.plan()
    sub.select("only", keep)
    f_keep, f_other, f_rest = tmp_path / "keep.txt", tmp_path / "other.txt", tmp_path / "rest.txt"
    f_keep.write_text("\n".join(keep))
    f_other.write_text("\n".join(other))
    f_rest.write_text("\n".join(ids[4:]))
    digest, flag = {
        "full_digest_with_subset_flag": (full.digest(), ["--only-proposals", str(f_keep)]),
        "subset_digest_without_flag": (sub.digest(), []),
        "subset_digest_with_other_subset": (sub.digest(), ["--only-proposals", str(f_other)]),
        "subset_digest_via_exclude": (sub.digest(), ["--exclude-proposals", str(f_rest)]),
    }[case]
    assert _run(script, "--out", str(tmp_path / "o"), "--write-proposals", "--expect-digest", digest,
                "--max-proposals", "60", *flag) == 2
    refused = [line for line in _lines(capsys) if line["section"] == "refused"]
    assert len(refused) == 1 and "proposal set changed" in refused[0]["reason"]


def test_a_stale_id_or_an_empty_file_refuses_the_run_with_exit_2(offline, tmp_path, capsys):
    script, cat = offline
    stale = "irp_" + "f" * 32
    f = tmp_path / "stale.txt"
    f.write_text("\n".join([_ids(cat.plan())[0], stale]))
    for mode in ("--only-proposals", "--exclude-proposals"):
        assert _run(script, "--out", str(tmp_path / "o"), mode, str(f)) == 2
        refused = [line for line in _lines(capsys) if line["section"] == "refused"]
        assert len(refused) == 1 and stale in refused[0]["reason"] and refused[0]["selection"] == f"{mode} {f}"
    assert not (tmp_path / "o").exists()           # nothing written for a refused selection

    async def must_not_read(*_a, **_kw):
        raise AssertionError("an empty selection is refused before the database is read")

    script.export_snapshot = must_not_read  # monkeypatch fixture restores it
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing labelled\n")
    assert _run(script, "--out", str(tmp_path / "o"), "--only-proposals", str(empty)) == 2
    assert "empty selection" in [line for line in _lines(capsys) if line["section"] == "refused"][0]["reason"]


def _outputs(capsys):
    return next(line for line in _lines(capsys) if line["section"] == "outputs")


@pytest.mark.parametrize("mode", ["only", "exclude"])
def test_the_file_and_inline_forms_of_a_mode_give_the_same_digest(offline, tmp_path, capsys, mode):
    script, cat = offline
    ids = _ids(cat.plan())
    chosen = ids[1:4]
    f = tmp_path / "clean.txt"
    f.write_text("\n".join(reversed(chosen)) + "\n" + chosen[0] + "\n")      # unsorted, duplicated
    assert _run(script, "--out", str(tmp_path / "a"), f"--{mode}-proposals", str(f)) == 0
    by_file = _outputs(capsys)
    inline = ",".join([chosen[2], f" {chosen[0]} ", chosen[1], chosen[0]])
    assert _run(script, "--out", str(tmp_path / "b"), f"--{mode}-proposal-ids", inline) == 0
    by_ids = _outputs(capsys)
    assert by_file["digest"] == by_ids["digest"] and by_file["proposals"] == by_ids["proposals"]
    assert by_file["next_step"].startswith(
        f"--write-proposals --expect-digest {by_file['digest']} --max-proposals {by_file['proposals']}"
        f" --{mode}-proposals {f} ")
    assert by_ids["next_step"].startswith(
        f"--write-proposals --expect-digest {by_ids['digest']} --max-proposals {by_ids['proposals']}"
        f" --{mode}-proposal-ids {','.join(sorted(chosen))} ")
    other = "exclude" if mode == "only" else "only"
    assert _run(script, "--out", str(tmp_path / "c"), f"--{other}-proposal-ids", ",".join(chosen)) == 0
    assert _outputs(capsys)["digest"] != by_ids["digest"]                 # the mode is folded in


def test_a_write_refuses_when_a_selected_proposals_content_changed_since_the_dry_run(offline, tmp_path, capsys):
    script, cat = offline
    chosen = _ids(cat.plan())[:3]
    assert _run(script, "--out", str(tmp_path / "a"), "--only-proposal-ids", ",".join(chosen)) == 0
    digest = _outputs(capsys)["digest"]
    cat.offers[1]["price"] = 25                     # same ids, a selected pair's evidence changed
    assert _ids(cat.plan())[:3] == chosen
    assert _run(script, "--out", str(tmp_path / "b"), "--write-proposals", "--expect-digest", digest,
                "--max-proposals", "3", "--only-proposal-ids", ",".join(chosen)) == 2
    assert "proposal set changed" in next(line for line in _lines(capsys) if line["section"] == "refused")["reason"]


@pytest.mark.parametrize("flag,value", [
    ("--only-proposals", "gs://bkt/cm/clean.txt"),
    ("--only-proposal-ids", " "),
    ("--exclude-proposal-ids", "irp_123"),
    ("--only-proposal-ids", "irp_" + "a" * 32 + ",,"),
])
def test_a_bad_selection_exits_2_before_the_database_is_read(offline, tmp_path, capsys, monkeypatch, flag, value):
    script, _cat = offline

    async def must_not_read(*_a, **_kw):
        raise AssertionError("a bad selection is refused before the database is read")

    monkeypatch.setattr(script, "export_snapshot", must_not_read)
    for mode in ([], ["--write-proposals", "--expect-digest", "cmd_x", "--max-proposals", "5"]):
        assert _run(script, "--out", str(tmp_path / "o"), *mode, flag, value) == 2
        refused = [line for line in _lines(capsys) if line["section"] == "refused"]
        assert len(refused) == 1
    assert not (tmp_path / "o").exists()
