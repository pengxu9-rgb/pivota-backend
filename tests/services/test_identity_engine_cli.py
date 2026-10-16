"""scripts/identity_engine.py: what an executed apply or revert rebuilds, and that it never writes
without the dry run's digest."""
from __future__ import annotations

from typing import Dict, List

import pytest

from scripts import identity_engine as cli


def test_moved_details_are_the_attaches_and_merges_a_revert_did_not_skip():
    details = [{"attached": "l", "from_content_key": "a", "to_content_key": "b"},
               {"merged": True, "from_content_key": "c", "to_content_key": "d"},
               {"merged": True, "from_content_key": "e", "to_content_key": "f", "reverted": False},
               {"suppressed": 2}]
    assert [d.get("from_content_key") for d in cli.moved_details(details)] == ["a", "c"]


@pytest.mark.asyncio
async def test_a_merge_rebuilds_the_losing_key_first(monkeypatch):
    import services.agent_pdp_view_assembler as apv
    import services.index_pipeline_state_service as ips
    from services.identity_brand_link import refresh_after_move

    seen: List[str] = []

    async def refresh(ck, **kw):
        seen.append(ck)
        return True

    async def nothing(ck, **kw):
        return False

    monkeypatch.setattr(apv, "refresh_agent_pdp_view_for_content_key", refresh)
    monkeypatch.setattr(apv, "delete_agent_pdp_view_if_orphaned", nothing)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", nothing)
    merged: Dict = {"merged": True, "from_content_key": "ck_loser", "to_content_key": "ck_winner"}
    await refresh_after_move(cli.moved_details([merged]), source="t")
    assert seen == ["ck_loser", "ck_winner"]
    seen.clear()
    await refresh_after_move(cli.moved_details([{**merged, "reverted": True}]), source="t")
    assert seen == ["ck_winner", "ck_loser"]


@pytest.mark.asyncio
async def test_execute_without_the_dry_runs_digest_is_refused_before_connecting(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        await cli.main(["apply", "--strategy", "s", "--max-proposals", "5", "--execute"])


# --- M3 review: the CLI's own governance --------------------------------------------------------------


class _Conn:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def fetch(self, sql, *args):
        return self.rows

    async def close(self):
        return None


@pytest.fixture
def engine(monkeypatch):
    """asyncpg and the engine stubbed: records how each entry point was called."""
    import asyncpg

    calls: Dict[str, list] = {"apply": [], "revert": [], "approve": [], "connect": []}
    conn = _Conn()

    async def connect(*a, **k):
        calls["connect"].append(a)
        return conn

    async def apply(c, **kw):
        calls["apply"].append(kw)
        return {"run_id": "R", "dry_run": kw.get("dry_run")}

    async def revert(c, pid, **kw):
        calls["revert"].append(kw)
        return {"proposal_id": pid, "dry_run": kw.get("dry_run")}

    async def approve(c, ids, by):
        calls["approve"].append(ids)
        return ids

    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/none")
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(cli, "apply_approved", apply)
    monkeypatch.setattr(cli, "revert_proposal", revert)
    monkeypatch.setattr(cli, "approve_proposals", approve)
    return calls, conn


@pytest.mark.asyncio
async def test_every_write_is_a_dry_run_unless_execute(engine):
    calls, conn = engine
    await cli.main(["apply", "--strategy", "coordinator_merge", "--max-proposals", "5"])
    await cli.main(["revert-proposal", "irp_x"])
    conn.rows = [{"proposal_id": "irp_x", "strategy": "coordinator_merge", "kind": "merge_content_key",
                  "status": "proposed"}]
    await cli.main(["approve", "irp_x", "--by", "peng"])
    assert calls["apply"][0]["dry_run"] is True and calls["revert"][0]["dry_run"] is True
    assert calls["approve"] == []
    await cli.main(["approve", "irp_x", "--by", "peng", "--execute"])
    assert calls["approve"] == [["irp_x"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [
    ["apply", "--strategy", "same_url_dup", "--max-proposals", "5"],
    ["apply", "--strategy", "retailer_listing_brand_prefix", "--max-proposals", "5"],
    ["propose-merge", "--from-ck", "a", "--to-ck", "b", "--keeper", "k", "--strategy", "junk_url"],
    ["propose-merge", "--from-ck", "a", "--to-ck", "b", "--keeper", "k", "--strategy", "variant_barcode_match"],
    ["propose-merge", "--from-ck", "a", "--to-ck", "b", "--keeper", "k", "--strategy", "cross_merchant_same_size",
     "--write"],
    ["apply", "--strategy", "cross_merchant_same_size", "--max-proposals", "5", "--execute"],  # no digest
    ["apply", "--strategy", "cross_merchant_same_size", "--expect-digest", "ird_x", "--execute"],  # no cap
])
async def test_a_reserved_strategy_name_is_refused_before_connecting(engine, argv):
    calls, _ = engine
    with pytest.raises(SystemExit):
        await cli.main(argv)
    assert calls["apply"] == [] and calls["connect"] == []


@pytest.mark.asyncio
async def test_approving_a_reserved_strategys_proposal_is_refused(engine):
    calls, conn = engine
    conn.rows = [{"proposal_id": "irp_s", "strategy": "same_url_dup", "kind": "merge_content_key",
                  "status": "proposed"}]
    with pytest.raises(SystemExit):
        await cli.main(["approve", "irp_s", "--by", "peng", "--execute"])
    assert calls["approve"] == []


def test_every_strategy_another_path_applies_by_name_is_reserved():
    """Pinned against the modules that write or auto-apply strategies, so a new one cannot slip by."""
    import re
    from pathlib import Path

    from services import identity_barcode_link, identity_brand_link
    from services.identity_reconcile_sweep import AUTO_APPROVE_STRATEGIES, JUDGE_STRATEGY

    root = Path(__file__).resolve().parents[2] / "services"
    written = set()
    for name in ("identity_resolution_strategies.py", "identity_tier3_judge.py", "identity_reconcile_sweep.py"):
        written |= set(re.findall(r'strategy\s*=\s*"([a-z0-9_]+)"', (root / name).read_text()))
    written |= {identity_brand_link.STRATEGY, identity_barcode_link.STRATEGY, JUDGE_STRATEGY,
                *AUTO_APPROVE_STRATEGIES}
    m4 = root / "identity_cross_merchant_match.py"  # the M4 module (its own branch): its STRATEGY too
    if m4.exists():
        written |= set(re.findall(r'^STRATEGY\s*=\s*"([a-z0-9_]+)"', m4.read_text(), re.M))
    assert written and written <= cli.RESERVED_STRATEGIES, written - cli.RESERVED_STRATEGIES



@pytest.mark.asyncio
async def test_a_module_only_strategy_is_applied_capped_and_digested_never_approved_by_hand(engine):
    """cross_merchant_same_size: only services/identity_cross_merchant_match.py writes its proposals.
    The coordinator may apply them here (dry run, then --execute with that run's digest, always capped);
    nobody may approve one here."""
    calls, conn = engine
    assert "cross_merchant_same_size" in cli.RESERVED_STRATEGIES
    await cli.main(["apply", "--strategy", "cross_merchant_same_size", "--max-proposals", "150"])
    await cli.main(["apply", "--strategy", "cross_merchant_same_size", "--max-proposals", "150",
                    "--expect-digest", "ird_abc", "--execute"])
    assert [(c["dry_run"], c["max_proposals"], c["expect_digest"], c["strategies"]) for c in calls["apply"]] == [
        (True, 150, None, ["cross_merchant_same_size"]),
        (False, 150, "ird_abc", ["cross_merchant_same_size"])]
    conn.rows = [{"proposal_id": "irp_m4", "strategy": "cross_merchant_same_size", "kind": "attach_membership",
                  "status": "proposed"}]
    with pytest.raises(SystemExit):
        await cli.main(["approve", "irp_m4", "--by", "peng", "--execute"])
    assert calls["approve"] == []


@pytest.mark.asyncio
async def test_build_merge_refuses_a_reserved_strategy_before_reading():
    class Boom:
        async def fetch(self, *a):
            raise AssertionError("read before the reservation check")

    with pytest.raises(SystemExit):
        await cli.build_merge(Boom(), from_ck="a", to_ck="b", keeper="k", strategy="cross_merchant_same_size")


@pytest.fixture
def module_run(engine, monkeypatch):
    calls, conn = engine
    calls["module_run"] = []

    async def approve_run(c, **kw):
        calls["module_run"].append(kw)
        return {"dry_run": kw["dry_run"]}

    monkeypatch.setattr(cli, "approve_module_run", approve_run)
    return calls


@pytest.mark.asyncio
async def test_an_m4_run_is_approved_only_by_run_and_digest(module_run):
    calls = module_run
    base = ["approve", "--strategy", "cross_merchant_same_size", "--by", "peng"]
    await cli.main([*base, "--run", "m4run_1"])
    await cli.main([*base, "--run", "m4run_1", "--expect-digest", "ira_x", "--execute"])
    assert [(c["proposal_run_id"], c["expect_digest"], c["dry_run"]) for c in calls["module_run"]] == [
        ("m4run_1", None, True), ("m4run_1", "ira_x", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [
    ["approve", "--strategy", "cross_merchant_same_size", "--by", "peng", "--execute"],                # no run
    ["approve", "--strategy", "cross_merchant_same_size", "--by", "peng", "--run", "r", "--execute"],  # no digest
    ["approve", "irp_1", "--strategy", "cross_merchant_same_size", "--by", "peng", "--run", "r"],      # by id
    ["approve", "--strategy", "retailer_listing_brand_prefix", "--by", "peng", "--run", "r"],          # not module-only
    ["approve", "irp_1", "--by", "peng", "--expect-digest", "ira_x", "--execute"],
])
async def test_every_other_approval_shape_for_a_module_strategy_is_refused(module_run, argv):
    with pytest.raises(SystemExit):
        await cli.main(argv)
    assert module_run["module_run"] == [] and module_run["approve"] == []


def test_the_sweep_and_the_link_scripts_never_name_the_module_strategy():
    from services import identity_barcode_link, identity_brand_link
    from services.identity_reconcile_sweep import AUTO_APPROVE_STRATEGIES, JUDGE_STRATEGY

    named = {*AUTO_APPROVE_STRATEGIES, JUDGE_STRATEGY, identity_brand_link.STRATEGY, identity_barcode_link.STRATEGY}
    assert not named & cli.MODULE_ONLY_STRATEGIES


#: Contract v3's offer and seed lists, well formed: one offer on a per-variant SKU, no seeds.
MATCH = {"matched_offers": [{"offer_id": "off_1", "sku_key": "ext:retailer:" + "a" * 32 + "::v:4500001"}],
         "matched_seeds": []}


@pytest.mark.parametrize("kind,verdict,ok", [
    ("attach_membership", {"strategy": "cross_merchant_same_size", "rule_version": "v1", "tier": "T0a",
                           "same_size": True, "size": {"value": 50, "unit": "ml", "pack": 2},
                           **MATCH}, True),
    ("merge_content_key", {"strategy": "cross_merchant_same_size", "rule_version": "v1", "tier": "T0c",
                           "same_size": True, "size": {"value": 50, "unit": "ml", "pack": 1},
                           **MATCH, "from_key_single_size": True}, True),
    ("attach_membership", {"strategy": "cross_merchant_same_size", "rule_version": " ", "tier": "T0a",
                           "same_size": True, "size": {"value": 50, "unit": "ml", "pack": 1},
                           **MATCH}, False),
    ("attach_membership", {"strategy": "cross_merchant_same_size", "rule_version": "v1", "tier": "T0a",
                           "same_size": True, "size": {"value": True, "unit": "ml", "pack": 1},
                           **MATCH}, False),
    ("attach_membership", {"strategy": "cross_merchant_same_size", "rule_version": "v1", "tier": "T0a",
                           "same_size": True, "size": {"value": 50, "unit": "", "pack": 0},
                           **MATCH}, False),
    ("attach_membership", {"strategy": "other", "rule_version": "v1", "tier": "T0a", "same_size": True,
                           "size": {"value": 50, "unit": "ml", "pack": 1}, **MATCH}, False),
    ("attach_membership", {"strategy": "cross_merchant_same_size", "rule_version": "v1", "tier": "T0a",
                           "same_size": True, "size": None, **MATCH}, False),
])
def test_the_shared_matcher_verdict_rule(kind, verdict, ok):
    from services.identity_resolution import validate_matcher_verdict

    assert (validate_matcher_verdict(kind, {"matcher_verdict": verdict}) == []) is ok



def test_the_shared_strategy_constant_is_the_one_spelling():
    from services import identity_resolution as ir

    assert ir.CROSS_MERCHANT_SAME_SIZE == "cross_merchant_same_size"
    assert ir.CROSS_MERCHANT_SAME_SIZE in cli.RESERVED_STRATEGIES
    assert cli.MODULE_ONLY_STRATEGIES == {ir.CROSS_MERCHANT_SAME_SIZE}


def test_hand_proposable_strategies_are_an_allowlist_disjoint_from_reserved():
    assert cli.HAND_PROPOSABLE_STRATEGIES and not cli.HAND_PROPOSABLE_STRATEGIES & cli.RESERVED_STRATEGIES


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["some_new_strategy", "cross_merchant_same_size", "same_url_dup"])
async def test_propose_merge_denies_any_strategy_not_on_the_allowlist(engine, strategy):
    calls, _ = engine
    with pytest.raises(SystemExit):
        await cli.main(["propose-merge", "--from-ck", "a", "--to-ck", "b", "--keeper", "k",
                        "--strategy", strategy, "--write"])
    assert calls["connect"] == []

    class Boom:
        async def fetch(self, *a):
            raise AssertionError("read before the allowlist check")

    with pytest.raises(SystemExit):
        await cli.build_merge(Boom(), from_ck="a", to_ck="b", keeper="k", strategy=strategy)



@pytest.mark.parametrize("unit,ok", [("ml", True), ("g", True), ("count", True), ("fl oz", False), ("oz", False),
                                     ("ML", False), ("", False), (None, False)])
def test_the_verdict_size_unit_is_one_of_be2635s_units(unit, ok):
    from services.identity_resolution import MATCHER_VERDICT_SIZE_UNITS, validate_matcher_verdict

    assert MATCHER_VERDICT_SIZE_UNITS == ("ml", "g", "count")
    verdict = {"strategy": "cross_merchant_same_size", "rule_version": "v1", "tier": "T0c", "same_size": True,
               "size": {"value": 50, "unit": unit, "pack": 1}, **MATCH}
    assert (validate_matcher_verdict("attach_membership", {"matcher_verdict": verdict}) == []) is ok



def _v3(**over):
    v = {"strategy": "cross_merchant_same_size", "rule_version": "v3", "tier": "T0a", "same_size": True,
         "size": {"value": 30, "unit": "ml", "pack": 1}, **MATCH}
    v.update(over)
    return {"matcher_verdict": v}


OFFER = {"offer_id": "off_1", "sku_key": "ext:retailer:" + "a" * 32 + "::v:4500001"}


@pytest.mark.parametrize("over,needle", [
    ({"matched_offers": None}, "matched_offers is not a non-empty list"),
    ({"matched_offers": []}, "matched_offers is not a non-empty list"),
    ({"matched_offers": "off_1"}, "matched_offers is not a non-empty list"),
    ({"matched_offers": ["off_1"]}, "matched_offers[0].offer_id"),
    ({"matched_offers": [{**OFFER, "offer_id": ""}]}, "matched_offers[0].offer_id"),
    ({"matched_offers": [{**OFFER, "sku_key": "ext:retailer:" + "a" * 32 + "::canonical"}]}, "per-variant"),
    ({"matched_offers": [{**OFFER, "sku_key": "prod::external_seed::external_seed::ext_bf55::canonical"}]},
     "per-variant"),  # the mirror's one-per-product key
    ({"matched_offers": [{**OFFER, "sku_key": "ext:retailer:" + "a" * 32}]}, "per-variant"),
    ({"matched_offers": [{**OFFER, "sku_key": "ext:retailer:" + "a" * 32 + "::v:1::canonical"}]}, "per-variant"),
    ({"matched_offers": [OFFER, {"offer_id": "off_2"}]}, "matched_offers[1].sku_key"),
    ({"size": {"value": 30, "unit": "ml", "pack": 0}}, "size.pack"),
    ({"size": {"value": 30, "unit": "ml", "pack": 1.5}}, "size.pack"),
    ({"size": {"value": 30, "unit": "ml", "pack": True}}, "size.pack"),
    ({"size": {"value": 30, "unit": "ml"}}, "size.pack"),
    ({"matched_seeds": None}, "matched_seeds is not a list"),
    ({"matched_seeds": [{"variant_id": "1"}]}, "matched_seeds[0].seed_id"),
    ({"matched_seeds": ["eps_1"]}, "matched_seeds[0].seed_id"),
    ({"matched_seeds": [{"seed_id": "eps_1", "variant_id": ""}]}, "matched_seeds[0].variant_id"),
    ({"matched_seeds": [{"seed_id": "eps_1", "variant_id": 45}]}, "matched_seeds[0].variant_id"),
])
def test_contract_v3_offer_and_seed_rules(over, needle):
    from services.identity_resolution import validate_matcher_verdict

    problems = validate_matcher_verdict("attach_membership", _v3(**over))
    assert any(needle in p for p in problems), problems


@pytest.mark.parametrize("over", [
    {},
    {"matched_offers": [{**OFFER, "sku_key": "ext:sku-identity-gate::0badc0de::v::4500001"}]},  # promoter spelling
    {"matched_offers": [OFFER, {**OFFER, "offer_id": "off_2"}]},
    {"matched_seeds": [{"seed_id": "eps_1", "variant_id": None}, {"seed_id": "eps_2", "variant_id": "4500001"}]},
])
def test_contract_v3_well_formed_verdicts(over):
    from services.identity_resolution import validate_matcher_verdict

    assert validate_matcher_verdict("attach_membership", _v3(**over)) == []


def test_the_v2_listing_offer_ids_field_no_longer_satisfies_the_rule():
    from services.identity_resolution import validate_matcher_verdict

    v2 = _v3()
    del v2["matcher_verdict"]["matched_offers"], v2["matcher_verdict"]["matched_seeds"]
    v2["matcher_verdict"]["listing_offer_ids"] = ["off_1"]
    problems = validate_matcher_verdict("attach_membership", v2)
    assert any("matched_offers" in p for p in problems) and any("matched_seeds" in p for p in problems)


@pytest.mark.parametrize("evidence,key", [
    (_v3(), (30.0, "ml", 1)),
    (_v3(size={"value": 1, "unit": "count", "pack": 3}), (1.0, "count", 3)),
    (_v3(size={"value": 30, "unit": "fl oz", "pack": 1}), None),
    (_v3(size={"value": 0, "unit": "ml", "pack": 1}), None),
    (_v3(size=None), None),
    ({}, None),
    (None, None),
])
def test_verdict_size_key(evidence, key):
    from services.identity_resolution import verdict_size_key

    assert verdict_size_key(evidence) == key
    if key:  # equal sizes written as int or float compare equal, for one-size-per-key checks
        same = _v3(size={"value": float(key[0]), "unit": key[1], "pack": key[2]})
        assert verdict_size_key(same) == key


def test_module_proposal_problems_needs_the_proposer_and_the_run():
    from services.identity_resolution import module_proposal_problems

    p = {"strategy": "cross_merchant_same_size", "kind": "attach_membership",
         "evidence": {**_v3(), "proposer": "cross_merchant_same_size", "proposal_run_id": "m4run_1"}}
    assert module_proposal_problems(p, "cross_merchant_same_size") == []
    for field, needle in (("proposer", "evidence.proposer"), ("proposal_run_id", "evidence.proposal_run_id")):
        bad = {**p, "evidence": {k: v for k, v in p["evidence"].items() if k != field}}
        assert any(needle in x for x in module_proposal_problems(bad, "cross_merchant_same_size"))
