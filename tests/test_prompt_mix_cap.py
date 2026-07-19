"""#1521 — audit prompt-mix rebalance: branded (product/brand-naming) prompts are
capped at a minority share of the per-SKU budget (default 30%, floor 2), the rest
rebalanced to unbranded discovery/category/problem/sidewalk shapes. Real buyers
overwhelmingly ask WITHOUT naming a product, so a branded-heavy set over-measures
~100% branded recall and under-measures the unbranded discovery demand that
decides AI visibility.

These tests pin: the budget math, the enforcement (cap + unbranded backfill +
last-resort branded restore), the env override, and the per-run telemetry.
"""

import pytest

from services import agent_center_bd_report_service as m
from services.prompt_basis import PROMPT_BASIS_VERSION


def _rec(query, axis):
    return {"query": query, "axis": axis}


# axis -> intent: intent/identity -> navigational (branded), review -> trust
# (branded); category -> category_head/problem_jtbd, sidewalk -> constraint
# (all unbranded).
_BRANDED = [
    _rec("where can I buy Acme Widget", "intent"),
    _rec("shop Acme Widget online", "intent"),
    _rec("is Acme legit", "review"),
    _rec("Acme Widget reviews", "review"),
    _rec("does Acme Widget actually work", "review"),
    _rec("Acme Widget 2-pack", "identity"),
]


def _unbranded_pool(n=40):
    return [_rec(f"best widget option {i}", "category") for i in range(n)] + [
        _rec(f"durable compact widget stack {i}", "sidewalk") for i in range(n)
    ]


# ---------------------------------------------------------------------------
# budget math
# ---------------------------------------------------------------------------


def test_branded_budget_is_capped_share_with_floor():
    # default cap 0.3, floor 2
    assert m._branded_prompt_budget(10) == 3      # 30%
    assert m._branded_prompt_budget(40) == 12     # 30%
    assert m._branded_prompt_budget(3) == 2       # int(0.9)=0 -> floor 2
    assert m._branded_prompt_budget(1) == 1       # floor clamped to target
    assert m._branded_prompt_budget(0) == 0


def test_is_branded_record_classification():
    assert m._is_branded_record(_rec("where can I buy X", "intent")) is True
    assert m._is_branded_record(_rec("X reviews", "review")) is True
    assert m._is_branded_record(_rec("X 2-pack", "identity")) is True
    assert m._is_branded_record(_rec("best widget", "category")) is False
    assert m._is_branded_record(_rec("compact widget", "sidewalk")) is False


# ---------------------------------------------------------------------------
# enforcement — cap + unbranded backfill
# ---------------------------------------------------------------------------


def _branded_count(records):
    return sum(1 for r in records if m._is_branded_record(r))


@pytest.mark.parametrize("target", [10, 40])
def test_mix_cap_enforced_at_10_and_40(target):
    # abundant unbranded available -> cap holds, budget fully filled, >=70% unbranded
    records = list(_BRANDED) + _unbranded_pool(target)
    out = m._enforce_prompt_mix(
        records, target=target, unbranded_backfill=_unbranded_pool(target)
    )
    assert len(out) == target
    branded = _branded_count(out)
    assert branded <= m._branded_prompt_budget(target)
    assert branded >= min(len(_BRANDED), 2)  # floor respected (enough branded exist)
    unbranded = len(out) - branded
    assert unbranded / len(out) >= 0.70


def test_floor_respected_and_baseline_kept_in_order():
    # cap keeps the FIRST branded records (navigational + trust baseline)
    records = list(_BRANDED) + _unbranded_pool(20)
    out = m._enforce_prompt_mix(
        records, target=10, unbranded_backfill=_unbranded_pool(20)
    )
    branded_kept = [r for r in out if m._is_branded_record(r)]
    assert len(branded_kept) == 3
    # the first three branded (2 navigational + 1 trust) are retained in order
    assert branded_kept[0]["query"] == "where can I buy Acme Widget"
    assert branded_kept[1]["query"] == "shop Acme Widget online"
    assert branded_kept[2]["query"] == "is Acme legit"


def test_surplus_branded_restored_when_unbranded_exhausted():
    # a genuinely branded-only thin SKU must NOT shrink its probe set: with no
    # unbranded to backfill, the surplus branded is restored to preserve coverage.
    records = list(_BRANDED)  # all branded, no unbranded anywhere
    out = m._enforce_prompt_mix(records, target=6, unbranded_backfill=[])
    assert len(out) == 6  # coverage preserved, not shrunk to the cap of 2
    assert _branded_count(out) == 6


def test_unbranded_preferred_over_restoring_branded():
    records = list(_BRANDED)
    out = m._enforce_prompt_mix(
        records, target=6, unbranded_backfill=_unbranded_pool(10)
    )
    assert len(out) == 6
    # cap=2 branded, remaining 4 filled from unbranded (not restored branded)
    assert _branded_count(out) == 2


# ---------------------------------------------------------------------------
# config knob (env override)
# ---------------------------------------------------------------------------


def test_cap_share_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_AUDIT_BRANDED_PROMPT_CAP", "0.5")
    assert m._branded_prompt_cap_share() == 0.5
    assert m._branded_prompt_budget(10) == 5
    # malformed / out-of-range fall back / clamp
    monkeypatch.setenv("AGENT_AUDIT_BRANDED_PROMPT_CAP", "nonsense")
    assert m._branded_prompt_cap_share() == m._BRANDED_PROMPT_CAP_DEFAULT
    monkeypatch.setenv("AGENT_AUDIT_BRANDED_PROMPT_CAP", "5")
    assert m._branded_prompt_cap_share() == 1.0


def test_floor_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_AUDIT_BRANDED_PROMPT_FLOOR", "3")
    assert m._branded_prompt_floor() == 3
    assert m._branded_prompt_budget(4) == 3  # floor 3 beats int(4*0.3)=1


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------


def test_prompt_mix_summary_counts_by_axis():
    records = [
        _rec("where can I buy X", "intent"),
        _rec("X reviews", "review"),
        _rec("best widget", "category"),
        _rec("best widget for travel", "category"),
        _rec("compact widget", "sidewalk"),
    ]
    summ = m._prompt_mix_summary(records)
    assert summ["branded"] == 2
    assert summ["unbranded"] == 3
    assert summ["total"] == 5
    assert summ["branded_share"] == 0.4
    assert summ["by_axis"]["navigational"] == 1
    assert summ["by_axis"]["trust"] == 1
    assert summ["by_axis"]["category_head"] == 1
    assert summ["by_axis"]["problem_jtbd"] == 1
    assert summ["by_axis"]["constraint"] == 1


def test_brand_prompt_mix_rolls_up_citation_by_intent():
    per_sku_reports = [
        {
            "citation_by_intent": {
                "navigational": {"total": 2, "cited": 2},
                "trust": {"total": 1, "cited": 1},
                "category_head": {"total": 4, "cited": 1},
                "constraint": {"total": 3, "cited": 0},
            }
        }
    ]
    mix = m._brand_prompt_mix(per_sku_reports)
    assert mix["branded"] == 3          # navigational + trust
    assert mix["unbranded"] == 7        # category_head + constraint
    assert mix["total"] == 10
    assert mix["branded_share"] == 0.3
    assert mix["unbranded_share"] == 0.7
    assert mix["by_axis"]["navigational"] == 2
    assert set(mix["branded_axes"]) == {"navigational", "trust"}
    assert mix["cap_share"] == m._BRANDED_PROMPT_CAP_DEFAULT
    assert mix["floor"] == m._BRANDED_PROMPT_FLOOR_DEFAULT


def test_brand_rollup_carries_prompt_mix_and_version_annotation():
    per_sku_reports = [
        {
            "sku_key": "s1",
            "citation_by_intent": {
                "navigational": {"total": 3, "cited": 3},
                "category_head": {"total": 7, "cited": 2},
            },
            "scores": {},
        }
    ]
    rollup = m.build_brand_rollup(per_sku_reports, "merch_1")
    assert "prompt_mix" in rollup
    assert rollup["prompt_mix"]["branded"] == 3
    assert rollup["prompt_mix"]["unbranded"] == 7
    assert rollup["prompt_mix_version"] == PROMPT_BASIS_VERSION
    assert "not directly comparable" in rollup["prompt_mix_note"].lower()


# ---------------------------------------------------------------------------
# integration — the real builder respects the cap on a rich SKU
# ---------------------------------------------------------------------------


def _rich_ctx():
    return {
        "sku_key": "widget-1",
        "product": {
            "title": "Acme Ultra Widget",
            "brand": "Acme",
            "product_type": "widget",
            "attributes_raw": {
                "materials": "aircraft aluminum",
                "use_case": "commuting and travel",
                "features": "compact, foldable, lightweight",
                "audience": "urban commuters",
            },
        },
        "sku": {"title": "2-pack"},
    }


@pytest.mark.parametrize("target", [10, 40])
def test_builder_respects_branded_cap_on_rich_sku(target):
    records = m._build_per_sku_audit_query_records(_rich_ctx(), target)
    branded = _branded_count(records)
    # branded never exceeds the cap of the per-SKU BUDGET (share of target)...
    assert branded <= m._branded_prompt_budget(target)
    assert branded >= 2  # identity/trust baseline preserved
    # ...and when the budget actually fills (enough unbranded demand exists — the
    # HoverAir-class case), >=70% of prompts are answerable without the brand.
    # A synthetic SKU that can't generate `target` unbranded prompts under-fills;
    # the >=70% guarantee at full budgets is pinned directly in
    # test_mix_cap_enforced_at_10_and_40.
    if len(records) >= target:
        assert (len(records) - branded) / len(records) >= 0.70
