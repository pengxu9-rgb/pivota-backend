"""cross_merchant_same_size (M4): --only-proposals / --exclude-proposals. Pure -- no database.

A hand-labelled subset of a dry run's proposals is the only thing a --write-proposals run may insert,
and the selection is folded into the digest: a subset's digest never authorises the full set, another
subset, or the same subset reached the other way round; the full set's never authorises a subset."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.identity_cross_merchant_match import (  # noqa: E402
    SelectionError,
    proposal_set_digest,
    select_proposals,
)
from test_identity_cross_merchant_match import _mixed  # noqa: E402


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


def test_the_subset_digest_still_pins_each_selected_proposals_evidence():
    cat = _mixed()
    plan = cat.plan()
    a = _ids(plan)[:3]
    plan.select("only", a)
    d_a = plan.digest()
    cat.offers[1]["price"] = 25        # one retailer's evidence changes
    again = cat.plan()
    changed = [p["proposal_id"] for p in again.proposals
               if p["proposal_id"] in a and p != next(q for q in plan.proposals if q["proposal_id"] == p["proposal_id"])]
    assert changed  # the fixture really moved a selected proposal
    again.select("only", a)
    assert again.digest() != d_a


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
    assert (sel["selected"], sel["of"], sel["ids_in_file"]) == (2, 6, 2)
    assert sel["run_digest"] == full.digest() and head["digest"] == plan.digest() != full.digest()
    samples = [line for line in plan.summary() if line["section"] == "samples"]
    assert samples and all(line["selection"] == {"mode": "only", "selected": 2, "of": 6} for line in samples)
    assert {p["proposal_id"] for line in samples for p in line["proposals"]} <= set(ids[:2])
    audit = plan.audit_rows()
    assert audit and {r["proposal_id"] for r in audit} <= set(ids[:2])
    assert all(r["selection"] == {"mode": "only", "selected": 2, "of": 6} for r in audit)
    # an unselected run says nothing about a selection
    assert "selection" not in full.summary()[0] and all("selection" not in r for r in full.audit_rows())


def test_new_multi_merchant_groups_are_recounted_for_the_subset():
    full = _mixed().plan()
    assert full.new_multi_merchant_groups == 1
    one = _ids(full)[:1]
    plan = _selected("only", one)
    assert plan.new_multi_merchant_groups == 1   # one retailer + the brand store = two hosts
    # sanity: an independently built plan agrees with itself after the same selection
    assert _selected("only", one).summary()[0]["expected_new_multi_merchant_groups"] == 1


# --- the ops script ---------------------------------------------------------------------------------------


def _script():
    from scripts.ops import propose_cross_merchant_matches as script
    return script


def test_only_and_exclude_are_mutually_exclusive(tmp_path):
    script = _script()
    f = tmp_path / "ids.txt"
    f.write_text("irp_x\n")
    with pytest.raises(SystemExit):
        script.parse_args(["--brands", "laneige", "--only-proposals", str(f), "--exclude-proposals", str(f)])
    with pytest.raises(SystemExit):
        script.parse_args(["--brands", "laneige", "--explain", "--only-proposals", str(f)])
    args = script.parse_args(["--brands", "laneige", "--only-proposals", str(f)])
    assert args.only_proposals == str(f) and args.exclude_proposals is None


def test_parse_proposal_ids_reads_lines_or_a_json_list():
    script = _script()
    assert script.parse_proposal_ids("irp_a\n\n# labelled clean\n  irp_b  \nirp_a\n") == ["irp_a", "irp_b"]
    assert script.parse_proposal_ids(' ["irp_b", "irp_a", "irp_b"] ') == ["irp_b", "irp_a"]
    assert script.parse_proposal_ids("\n# nothing\n") == []
    for bad in ('["irp_a", 3]', '["irp_a", ""]', "[irp_a"):
        with pytest.raises(script.Refused):
            script.parse_proposal_ids(bad)


def test_load_selection_reads_a_local_or_gcs_file_and_refuses_an_empty_or_missing_one(tmp_path):
    script = _script()
    f = tmp_path / "ids.json"
    f.write_text('["irp_a", "irp_b"]')
    args = script.parse_args(["--brands", "laneige", "--exclude-proposals", str(f)])
    assert script.load_selection(args) == ("exclude", ["irp_a", "irp_b"], str(f))
    assert script.load_selection(script.parse_args(["--brands", "laneige"])) is None
    got = []
    gs = script.parse_args(["--brands", "laneige", "--only-proposals", "gs://bkt/cm/clean.txt"])
    sel = script.load_selection(gs, downloader=lambda b, n: (got.append((b, n)), b"irp_a\n")[1])
    assert sel == ("only", ["irp_a"], "gs://bkt/cm/clean.txt") and got == [("bkt", "cm/clean.txt")]
    empty = tmp_path / "empty.txt"
    empty.write_text("\n\n")
    with pytest.raises(script.Refused, match="empty selection"):
        script.load_selection(script.parse_args(["--brands", "laneige", "--only-proposals", str(empty)]))
    with pytest.raises(script.Refused, match="cannot read"):
        script.load_selection(script.parse_args(["--brands", "laneige", "--only-proposals", str(tmp_path / "nope")]))


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
        assert len(refused) == 1 and stale in refused[0]["reason"] and refused[0]["selection_file"] == str(f)
    assert not (tmp_path / "o").exists()           # nothing written for a refused selection

    async def must_not_read(*_a, **_kw):
        raise AssertionError("an empty selection is refused before the database is read")

    script.export_snapshot = must_not_read  # monkeypatch fixture restores it
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing labelled\n")
    assert _run(script, "--out", str(tmp_path / "o"), "--only-proposals", str(empty)) == 2
    assert "empty selection" in [line for line in _lines(capsys) if line["section"] == "refused"][0]["reason"]
