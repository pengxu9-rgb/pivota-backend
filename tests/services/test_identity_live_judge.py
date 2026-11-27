"""The live judge run offline (services/identity_live_judge.py, scripts/ops/run_identity_judge_offline.py).
Synthetic data only: placeholder brands and example.* hosts, never a real store or product."""

import json
import os

import pytest

from scripts.ops import run_identity_judge_offline as cli
from services import identity_live_judge as lj
from services import identity_truth_set as ts

REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def side(title, brand="Brand Alpha", host="store-a.example", **kw):
    return {"brand": brand, "title": title, "host": host, "url": f"https://{host}/p", **kw}


def view(a, b, pid="tsp_test"):
    return {"pair_id": pid, "a": a, "b": b}


def answer(same=True, cites=("a.title", "b.title"), **fields):
    f = {"brand": "agree", "line": "agree", "size": "agree", "shade": "not_applicable", "form": "agree"}
    f.update(fields)
    return {"same": same, "fields": f, "cites": list(cites)}


def const(ans):
    return lambda v: ans


SAME_A = side("Glow Serum 30ml")
SAME_B = side("Glow Serum 30 ml", host="store-b.example")


# --- vetoes ----------------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,reason", [
    (side("Glow Serum 30ml"), side("Glow Serum 50ml", host="store-b.example"), "veto:size"),
    (side("Glow Serum 30ml"), side("Glow Serum 30ml", brand="Brand Beta", host="store-b.example"), "veto:brand"),
    (side("Lip Tint 4g - Rose"), side("Lip Tint 4g - Coral", host="store-b.example"), "veto:shade"),
    (side("Glow Serum 30ml"), side("Glow Serum Refill 30ml", host="store-b.example"), "veto:conflict:refill"),
    (side("Glow Serum 30ml", price=20, currency="USD"),
     side("Glow Serum 30ml", host="store-b.example", price=40, currency="USD"), "veto:price_band"),
])
def test_a_stated_conflict_ends_the_pair_before_any_agent(a, b, reason):
    called = []
    def agent(v):
        called.append(v)
        return answer()
    r = lj.judge(view(a, b), agent, agent)
    assert r["verdict"] == "not_shown" and reason in r["reasons"]
    assert called == []  # no agent overrides a veto: it never sees the pair


def test_missing_information_is_not_a_veto():
    a, b = side("Glow Serum"), side("Glow Serum 30ml", host="store-b.example")
    assert lj.vetoes(lj.normalise(a), lj.normalise(b)) == []
    # one-sided shade, prices in two currencies, one price missing: no veto either
    a, b = side("Lip Tint 4g - Rose"), side("Lip Tint 4g", host="store-b.example")
    assert lj.vetoes(lj.normalise(a), lj.normalise(b)) == []
    a = side("Glow Serum 30ml", price=20, currency="USD")
    b = side("Glow Serum 30ml", host="store-b.example", price=60, currency="EUR")
    assert lj.vetoes(lj.normalise(a), lj.normalise(b)) == []
    b = side("Glow Serum 30ml", host="store-b.example")
    assert lj.vetoes(lj.normalise(a), lj.normalise(b)) == []


def test_the_price_band_edges_are_inside():
    for pb in (12.0, 33.4):  # 20/12 = 1.667 and 20/33.4 = 0.599: just inside / just outside
        a = side("Glow Serum 30ml", price=20, currency="USD")
        b = side("Glow Serum 30ml", host="store-b.example", price=pb, currency="USD")
        assert ("price_band" in lj.vetoes(lj.normalise(a), lj.normalise(b))) is (pb == 33.4)


# --- combining the two agents ----------------------------------------------------------------

def test_both_agree_every_field_checks_is_confirmed():
    r = lj.judge(view(SAME_A, SAME_B), const(answer()), const(answer()))
    assert r["verdict"] == "confirmed" and r["reasons"] == []


def test_a_field_unknown_caps_at_likely():
    r = lj.judge(view(SAME_A, SAME_B), const(answer()), const(answer(shade="unknown")))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:shade"]


@pytest.mark.parametrize("sceptic,reason", [
    (answer(same=False), "sceptic:different"),
    (answer(form="differ"), "sceptic:differ:form"),
])
def test_disagreement_or_a_differing_field_is_not_shown(sceptic, reason):
    r = lj.judge(view(SAME_A, SAME_B), const(answer()), const(sceptic))
    assert r["verdict"] == "not_shown" and reason in r["reasons"]


def test_a_missing_or_failing_agent_is_not_shown():
    r = lj.judge(view(SAME_A, SAME_B), const(answer()), const(None))
    assert r == {**r, "verdict": "not_shown", "reasons": ["sceptic:no_answer"]}
    def boom(v):
        raise TimeoutError("deadline")
    r = lj.judge(view(SAME_A, SAME_B), boom, const(answer()))
    assert r["verdict"] == "not_shown" and r["reasons"] == ["matcher:error:TimeoutError"]


# --- the verifier and grounding --------------------------------------------------------------

@pytest.mark.parametrize("bad,why", [
    (answer(cites=["a.title", "b.image_url"]), "does not have"),  # b has no image
    (answer(cites=["a.title", "c.title"]), "is not a.<field>"),
    (answer(cites=["a.gtin", "b.title"]), "is not a.<field>"),   # the barcode is never citable
    (answer(cites=["a.title"]), "cite both"),
    ({**answer(), "same": "yes"}, "not a boolean"),
    (answer(size="maybe"), "has value"),
    (answer(line="not_applicable"), "cannot be not_applicable"),
])
def test_an_answer_that_breaks_the_contract_is_dropped(bad, why):
    r = lj.judge(view(SAME_A, SAME_B), const(answer()), const(bad))
    assert r["verdict"] == "not_shown"
    assert len(r["reasons"]) == 1 and r["reasons"][0].startswith("sceptic:dropped:") and why in r["reasons"][0]


def test_an_agent_cannot_create_size_or_brand_agreement_it_cannot_read():
    a, b = side("Glow Serum"), side("Glow Serum", host="store-b.example")  # no size on either side
    r = lj.judge(view(a, b), const(answer()), const(answer()))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:size"]
    a, b = side("Glow Serum 30ml", brand=""), side("Glow Serum 30ml", host="store-b.example")
    r = lj.judge(view(a, b), const(answer(cites=["a.title", "b.title"])), const(answer()))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:brand"]


def test_a_different_answer_needs_no_cites():
    r = lj.judge(view(SAME_A, SAME_B), const(answer()), const(answer(same=False, cites=[])))
    assert r["reasons"] == ["sceptic:different"]


# --- the deterministic stand-ins -------------------------------------------------------------

def test_rules_agents_confirm_only_what_they_can_read():
    r = lj.judge(view(SAME_A, SAME_B), lj.rules_matcher, lj.rules_sceptic)
    assert r["verdict"] == "confirmed"
    reordered = side("Serum Glow 30ml", host="store-b.example")
    r = lj.judge(view(SAME_A, reordered), lj.rules_matcher, lj.rules_sceptic)
    assert r["verdict"] == "not_shown" and r["reasons"] == ["sceptic:different", "sceptic:differ:line"]
    r = lj.judge(view(side("Glow Serum"), side("Glow Serum", host="store-b.example")),
                 lj.rules_matcher, lj.rules_sceptic)
    assert r["verdict"] == "likely"


def test_variant_title_is_read():
    a = side("Glow Serum", variant_title="30ml")
    b = side("Glow Serum", host="store-b.example", variant_title="50ml")
    assert lj.judge(view(a, b), lj.rules_matcher, lj.rules_sceptic)["reasons"] == ["veto:size"]


def test_replay_agent_reads_its_role_only():
    rows = [{"pair_id": "p1", "role": "matcher", "answer": answer()},
            {"pair_id": "p1", "role": "sceptic", "answer": answer(same=False)}]
    m, s = lj.ReplayAgent("matcher", rows), lj.ReplayAgent("sceptic", rows)
    assert m({"pair_id": "p1"})["same"] is True and s({"pair_id": "p1"})["same"] is False
    assert m({"pair_id": "p2"}) is None
    with pytest.raises(ValueError):
        lj.ReplayAgent("judge", rows)


# --- the model text contract -----------------------------------------------------------------

def test_prompt_shows_only_citable_fields():
    v = view({**SAME_A, "gtin": "0000000000000", "listing_key": "k1"}, SAME_B)
    p = lj.build_agent_prompt("sceptic", v)
    assert "gtin" not in p and "k1" not in p and "Glow Serum 30ml" in p and "NOT the same" in p
    with pytest.raises(ValueError):
        lj.build_agent_prompt("judge", v)


def test_parse_agent_answer_takes_the_first_object():
    obj = answer()
    assert lj.parse_agent_answer(f"Here you go:\n```json\n{json.dumps(obj)}\n```") == obj
    assert lj.parse_agent_answer("{not json} then " + json.dumps(obj)) == obj
    assert lj.parse_agent_answer("no object") is None
    assert lj.parse_agent_answer(None) is None


def test_reason_counts_cut_dropped_free_text():
    results = [{"reasons": ["sceptic:dropped:cite 'a.x' is not a.<field>"]},
               {"reasons": ["sceptic:dropped:same is not a boolean", "unknown:size"]}]
    assert lj.reason_counts(results) == {"sceptic:dropped": 2, "unknown:size": 1}


# --- the CLI end to end ----------------------------------------------------------------------

def _truth(tmp_path):
    pairs = [
        ts.TruthPair(pair_id="tsp_same", a={"listing_key": "k1", **SAME_A}, b={"listing_key": "k2", **SAME_B},
                     label="same", grade="silver", basis="barcode_clean", source="test"),
        ts.TruthPair(pair_id="tsp_diff", a={"listing_key": "k3", **side("Glow Serum 30ml")},
                     b={"listing_key": "k4", **side("Glow Serum 50ml", host="store-b.example")},
                     label="different", grade="silver", basis="size_variant", source="test"),
    ]
    path = tmp_path / "truth.jsonl"
    ts.write_jsonl(str(path), (ts.to_row(p) for p in pairs), repo_root=REPO)
    return path


def test_cli_run_scores_and_writes_outside_the_repo(tmp_path, capsys):
    truth, out = _truth(tmp_path), tmp_path / "verdicts.jsonl"
    assert cli.main(["run", "--truth", str(truth), "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["score"]["gate"] == "pass" and printed["written"] == 2
    assert printed["by_basis"] == {"silver/different/size_variant": {"not_shown": 1},
                                   "silver/same/barcode_clean": {"confirmed": 1}}
    assert {r["pair_id"]: r["verdict"] for r in ts.read_jsonl(str(out))} == {
        "tsp_same": "confirmed", "tsp_diff": "not_shown"}


def test_cli_gate_fails_on_a_wrong_confirmed(tmp_path, capsys):
    truth = _truth(tmp_path)
    replay = tmp_path / "answers.jsonl"
    # The size veto still stops the "different" pair, so lie on the same pair's label instead.
    rows = [ts.to_row(p) for p in map(ts.from_row, ts.read_jsonl(str(truth)))]
    rows[0]["label"] = "different"
    ts.write_jsonl(str(truth), rows, repo_root=REPO)
    ts.write_jsonl(str(replay), [{"pair_id": "tsp_same", "role": r, "answer": answer()} for r in lj.ROLES],
                   repo_root=REPO)
    spec = f"replay:{replay}"
    rc = cli.main(["run", "--truth", str(truth), "--matcher", spec, "--sceptic", spec,
                   "--out", str(tmp_path / "v.jsonl")])
    printed = json.loads(capsys.readouterr().out)
    assert rc == 1 and printed["score"]["wrong_confirmed"] == ["tsp_same"]
    assert printed["reasons"] == {"veto:size": 1}


def test_cli_prompts_and_refusals(tmp_path, capsys):
    truth = _truth(tmp_path)
    out = tmp_path / "prompts.jsonl"
    assert cli.main(["prompts", "--truth", str(truth), "--role", "matcher", "--out", str(out)]) == 0
    rows = ts.read_jsonl(str(out))
    assert [r["role"] for r in rows] == ["matcher", "matcher"] and "k1" not in rows[0]["prompt"]
    capsys.readouterr()
    inside = os.path.join(REPO, "verdicts.jsonl")
    assert cli.main(["run", "--truth", str(truth), "--out", inside]) == 2
    assert json.loads(capsys.readouterr().out)["judge_offline"] == "refused"
    assert not os.path.exists(inside)
    assert cli.main(["run", "--truth", str(truth), "--matcher", "gpt", "--out", str(tmp_path / "v")]) == 2
