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


SAME_A = side("Glow Serum 30ml", price=20, currency="USD")
SAME_B = side("Glow Serum 30 ml", host="store-b.example", price=22, currency="USD")


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


@pytest.mark.parametrize("pa,vetoed", [(0.6, False), (1.67, False), (0.599, True), (1.671, True)])
def test_the_price_band_edges_are_inside(pa, vetoed):
    a = side("Glow Serum 30ml", price=pa, currency="USD")
    b = side("Glow Serum 30ml", host="store-b.example", price=1, currency="USD")
    assert ("price_band" in lj.vetoes(lj.normalise(a), lj.normalise(b))) is vetoed


@pytest.mark.parametrize("pa,ca,pb,cb", [
    (20, "USD", 200, "EUR"),      # two currencies: never converted
    (20, None, 200, None),        # no currency on either side: never assumed
    (20, "USD", None, "USD"),     # one price missing
    ("$20", "USD", 21, "USD"),    # not a plain number: never guessed
    (float("nan"), "USD", 21, "USD"),
    ("nan", "USD", 21, "USD"),
    (float("inf"), "USD", 21, "USD"),
    ("1e1", "USD", 21, "USD"),    # exponent spelling is not a plain number
    (-20, "USD", 21, "USD"),
    (True, "USD", 21, "USD"),
])
def test_an_unchecked_price_caps_at_likely(pa, ca, pb, cb):
    a = side("Glow Serum 30ml", price=pa, currency=ca)
    b = side("Glow Serum 30ml", host="store-b.example", price=pb, currency=cb)
    r = lj.judge(view(a, b), const(answer()), const(answer()))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:price"]


def test_currency_is_compared_stripped_and_case_folded():
    a = side("Glow Serum 30ml", price=20, currency=" usd")
    b = side("Glow Serum 30ml", host="store-b.example", price=200, currency="USD ")
    assert lj.judge(view(a, b), const(answer()), const(answer()))["reasons"] == ["veto:price_band"]
    b = side("Glow Serum 30ml", host="store-b.example", price=" 21 ", currency="USD")
    assert lj.judge(view(a, b), const(answer()), const(answer()))["verdict"] == "confirmed"


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
    (answer(cites=["", "a.title", "b.title"]), "is not a.<field>"),
    ({**answer(), "note": "x"}, "unknown keys"),
    ({**answer(), "fields": {**answer()["fields"], "colour": "agree"}}, "unknown fields"),
    ({**answer(), "same": "yes"}, "not a boolean"),
    (answer(size="maybe"), "has value"),
    (answer(line="not_applicable"), "cannot be not_applicable"),
])
def test_an_answer_that_breaks_the_contract_is_dropped(bad, why):
    r = lj.judge(view(SAME_A, SAME_B), const(answer()), const(bad))
    assert r["verdict"] == "not_shown"
    assert len(r["reasons"]) == 1 and r["reasons"][0].startswith("sceptic:dropped:") and why in r["reasons"][0]


USD = {"price": 20, "currency": "USD"}


def test_an_agent_cannot_create_size_or_brand_agreement_it_cannot_read():
    a, b = side("Glow Serum", **USD), side("Glow Serum", host="store-b.example", **USD)  # no size either side
    r = lj.judge(view(a, b), const(answer()), const(answer()))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:size"]
    a, b = side("Glow Serum 30ml", brand="", **USD), side("Glow Serum 30ml", host="store-b.example", **USD)
    r = lj.judge(view(a, b), const(answer()), const(answer()))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:brand"]
    a, b = side("Glow Serum 30ml", brand="", **USD), side("Glow Serum 30ml", brand="", host="store-b.example", **USD)
    r = lj.judge(view(a, b), const(answer()), const(answer()))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:brand"]


@pytest.mark.parametrize("shade_answer", ["agree", "not_applicable"])
def test_a_shade_on_one_side_only_caps_at_likely(shade_answer):
    a = side("Lip Tint 4g - Rose", **USD)
    b = side("Lip Tint 4g", host="store-b.example", **USD)
    r = lj.judge(view(a, b), const(answer(shade=shade_answer)), const(answer(shade=shade_answer)))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:shade"]


def test_shade_agrees_only_on_equal_readings_and_is_not_applicable_only_on_none():
    a = side("Lip Tint 4g - Rose", **USD)
    b = side("Lip Tint 4g - Rose", host="store-b.example", **USD)
    assert lj.judge(view(a, b), const(answer(shade="agree")), const(answer(shade="agree")))["verdict"] == "confirmed"
    r = lj.judge(view(a, b), const(answer(shade="not_applicable")), const(answer(shade="agree")))
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:shade"]


def test_a_different_answer_needs_no_cites():
    r = lj.judge(view(SAME_A, SAME_B), const(answer()), const(answer(same=False, cites=[])))
    assert r["reasons"] == ["sceptic:different"]


# --- the deterministic stand-ins -------------------------------------------------------------

def test_rules_agents_confirm_only_what_they_can_read():
    r = lj.judge(view(SAME_A, SAME_B), lj.rules_matcher, lj.rules_sceptic)
    assert r["verdict"] == "confirmed"
    reordered = side("Serum Glow 30ml", host="store-b.example", **USD)
    r = lj.judge(view(SAME_A, reordered), lj.rules_matcher, lj.rules_sceptic)
    assert r["verdict"] == "not_shown" and r["reasons"] == ["sceptic:different", "sceptic:differ:line"]
    r = lj.judge(view(side("Glow Serum", **USD), side("Glow Serum", host="store-b.example", **USD)),
                 lj.rules_matcher, lj.rules_sceptic)
    assert r["verdict"] == "likely" and r["reasons"] == ["unknown:size"]


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
    with pytest.raises(ValueError, match="two matcher answers"):
        lj.ReplayAgent("matcher", rows + [{"pair_id": "p1", "role": "matcher", "answer": answer(same=False)}])


def test_a_view_that_is_not_two_objects_is_refused():
    for bad in ("x", {"pair_id": "p", "a": "a title", "b": SAME_B}, {"pair_id": "p", "a": SAME_A}):
        with pytest.raises(ValueError):
            lj.judge(bad, const(answer()), const(answer()))


# --- the model text contract -----------------------------------------------------------------

def test_prompt_shows_only_citable_fields():
    v = view({**SAME_A, "gtin": "0000000000000", "listing_key": "k1"}, SAME_B)
    p = lj.build_agent_prompt("sceptic", v)
    assert "gtin" not in p and "k1" not in p and "Glow Serum 30ml" in p and "NOT the same" in p
    with pytest.raises(ValueError):
        lj.build_agent_prompt("judge", v)


def test_parse_agent_answer_takes_the_object_at_the_first_brace():
    obj = answer()
    assert lj.parse_agent_answer(f"Here you go:\n```json\n{json.dumps(obj)}\n```") == obj
    assert lj.parse_agent_answer("{not json} then " + json.dumps(obj)) is None  # no later object is tried
    assert lj.parse_agent_answer("no object") is None
    assert lj.parse_agent_answer(None) is None


def test_parse_agent_answer_cannot_flip_false_to_true():
    inner = json.dumps(answer(same=True))
    # A brace inside a string is text: the outer false answer is read, never the inner true one.
    text = '{"same": false, "note": "x}", "ex": ' + inner + "}"
    assert lj.parse_agent_answer(text)["same"] is False
    # A broken outer object is refused whole; its nested true object is not taken instead.
    assert lj.parse_agent_answer('{"same": false, "ex": ' + inner + ", broken}") is None


def test_parse_agent_answer_refuses_duplicate_keys():
    assert lj.parse_agent_answer('{"same": false, "same": true, "fields": {}, "cites": []}') is None
    assert lj.parse_agent_answer('{"same": true, "fields": {"size": "differ", "size": "agree"}, "cites": []}') is None


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


def test_cli_refuses_a_side_that_is_not_an_object(tmp_path, capsys):
    truth = _truth(tmp_path)
    rows = ts.read_jsonl(str(truth))
    rows[0]["a"] = "a title"
    ts.write_jsonl(str(truth), rows, repo_root=REPO)
    assert cli.main(["run", "--truth", str(truth), "--out", str(tmp_path / "v.jsonl")]) == 2
    assert "not an object" in json.loads(capsys.readouterr().out)["error"]


def test_outputs_are_refused_inside_any_git_checkout(tmp_path):
    other = tmp_path / "other-checkout"
    (other / "sub").mkdir(parents=True)
    (other / ".git").write_text("gitdir: elsewhere\n")  # a worktree's .git is a file
    with pytest.raises(ts.TruthSetError, match="inside a git checkout"):
        ts.write_jsonl(str(other / "sub" / "v.jsonl"), [{"x": 1}], repo_root=REPO)
    assert not (other / "sub" / "v.jsonl").exists()
