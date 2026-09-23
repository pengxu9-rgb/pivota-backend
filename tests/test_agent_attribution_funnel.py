import re
from datetime import datetime, timezone

from scripts import agent_attribution_funnel as f

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)
ALL_COLS = ["id", "agent_id", "state", "currency", "final_total_minor", "reap_order_id",
            "merchant_domain", "last_error_code", "terminal_at", "created_at", "item_source"]


def test_the_source_can_be_passed_inline_to_the_oneoff_runner():
    src = open(f.__file__).read()
    # run_oneoff_job.sh picks its --args delimiter from characters absent in the payload.
    assert "@" not in src


def test_every_query_is_read_only():
    writes = re.compile(r"\b(insert|update|delete|alter|drop|truncate|create|grant|copy)\b", re.I)
    for name, sql in f.build_queries(ALL_COLS).items():
        assert sql.lstrip().upper().startswith("SELECT"), name
        assert not writes.search(sql), name
    assert not writes.search(f.purchase_columns_sql())


def test_without_the_purchases_table_only_the_referral_queries_run():
    assert set(f.build_queries([])) == {"clicks", "edges"}


def test_a_prod_without_item_source_still_reports_the_partner_lane():
    q = f.build_queries([c for c in ALL_COLS if c != "item_source"])["purchases"]
    assert "p.item_source" not in q
    assert "'reap_variant' AS item_source" in q
    assert "coalesce(p.item_source" in f.build_queries(ALL_COLS)["purchases"]


def test_the_purchase_edge_join_is_the_stamped_purchase_id():
    q = f.build_queries(ALL_COLS)
    assert "partner_provenance' ->> 'purchase_id' = p.id" in q["completed_without_edge"]


def _rows():
    return {
        "clicks": [
            {"agent": "agent_minds", "surface": "reap_cart_link", "issued": 3, "clicked": 2},
            {"agent": f.NO_AGENT, "surface": "offers.resolve", "issued": 5, "clicked": 1},
        ],
        "purchases": [
            {"agent": "agent_minds", "item_source": "reap_variant", "state": "completed", "currency": "USD", "n": 2, "completed_minor": 9000},
            {"agent": "agent_minds", "item_source": "cart_link", "state": "awaiting_approval", "currency": "USD", "n": 1, "completed_minor": 0},
            {"agent": "agent_minds", "item_source": "reap_variant", "state": "failed", "currency": "USD", "n": 1, "completed_minor": 0},
            {"agent": "agent_b", "item_source": "reap_variant", "state": "completed", "currency": "SGD", "n": 1, "completed_minor": 3000},
        ],
        "edges": [
            {"agent": "agent_minds", "agent_source": "partner_purchase", "partner": True, "state": "converted", "currency": "USD", "n": 1, "gmv_minor": 4500, "refunded_edges": 1, "refunded_amount": 10},
            {"agent": f.NO_AGENT, "agent_source": "", "partner": True, "state": "converted", "currency": "SGD", "n": 1, "gmv_minor": 3000, "refunded_edges": 0, "refunded_amount": 0},
            # Unconverted legacy edges (state NULL in prod) are not credit.
            {"agent": "agent_legacy", "agent_source": "", "partner": False, "state": "", "currency": "", "n": 15, "gmv_minor": 17500, "refunded_edges": 0, "refunded_amount": 0},
        ],
        "completed_without_edge": [
            {"purchase_id": "rp_2", "agent": "agent_minds", "merchant": "brand.example", "order_id": "o2", "final_minor": 4500, "currency": "USD", "last_error_code": None, "terminal_at": None, "reason": "missing_edge"},
            {"purchase_id": "rp_9", "agent": "agent_b", "merchant": "shop.sg", "order_id": None, "final_minor": 3000, "currency": "SGD", "last_error_code": "completed_without_order_id", "terminal_at": None, "reason": "no_order_id"},
        ],
        "partner_edge_agent_check": [
            {"edge_id": "e_none", "edge_agent": "", "purchase_agent": "agent_b", "purchase_id": "rp_8", "created_at": None},
            {"edge_id": "e_mm", "edge_agent": "agent_x", "purchase_agent": "agent_minds", "purchase_id": "rp_3", "created_at": None},
        ],
        "_errors": {},
    }


def test_the_funnel_folds_each_lane_per_agent():
    fn = f.build_funnel(_rows(), 30, now=NOW)
    by = {a["agent"]: a for a in fn["agents"]}
    m = by["agent_minds"]
    assert (m["issued"], m["clicked"]) == (3, 2)
    assert (m["opened"], m["completed"], m["in_flight"], m["failed"]) == (4, 2, 1, 1)
    assert (m["credited"], m["credited_partner"]) == (1, 1)
    assert m["credited_minor"] == {"USD": 4500}
    assert m["refunded_edges"] == 1
    assert m["lanes"] == {"reap_variant": 3, "cart_link": 1}
    # An unconverted edge (state NULL on the 2026-03 test edges in prod) is not credit, and on
    # its own it does not make an agent appear.
    assert "agent_legacy" not in by
    # Named agents sort before the no-agent bucket.
    assert fn["agents"][-1]["agent"] == f.NO_AGENT
    assert fn["agents"][0]["agent"] == "agent_minds"


def test_an_edge_with_no_agent_is_never_counted_as_credited_to_an_agent():
    t = f.build_funnel(_rows(), 30, now=NOW)["totals"]
    assert t["credited_partner"] == 2
    assert t["credited_partner_to_agent"] == 1


def test_the_integrity_exceptions_are_split_and_reported():
    ex = f.build_funnel(_rows(), 30, now=NOW)["exceptions"]
    assert ex["completed_without_edge"]["count"] == 2
    assert ex["completed_without_edge"]["by_reason"] == {"missing_edge": 1, "no_order_id": 1}
    assert [r["edge_id"] for r in ex["partner_edge_without_agent"]["rows"]] == ["e_none"]
    assert [r["edge_id"] for r in ex["agent_mismatch"]["rows"]] == ["e_mm"]


def test_totals_cover_agents_beyond_the_display_cap(monkeypatch):
    monkeypatch.setattr(f, "MAX_AGENTS", 1)
    fn = f.build_funnel(_rows(), 30, now=NOW)
    assert len(fn["agents"]) == 1
    assert fn["totals"]["opened"] == 5
    assert fn["totals"]["agents_truncated"] == 2


def test_render_names_every_section_and_flags_query_errors():
    rows = _rows()
    rows["_errors"] = {"edges": "UndefinedColumn x"}
    out = f.render(f.build_funnel(rows, 7, now=NOW))
    assert "last 7d" in out
    assert "credited to an agent 1 (partner edges: 2)" in out
    assert "rp_2" in out and "reason=missing_edge" in out
    assert "e_none" in out and "e_mm" in out
    assert "USD 45.00" in out
    assert "QUERY ERRORS" in out


def test_render_says_when_the_partner_lane_is_unmeasured():
    rows = {"clicks": [], "edges": [], "_errors": {}}
    assert "partner lane is not measured" in f.render(f.build_funnel(rows, 30, now=NOW))


def test_inline_invocation_honours_its_arguments(monkeypatch):
    # `python -c "$(cat file)" --days 7` puts ["-c", "--days", "7"] in sys.argv; the report must
    # read --days from it exactly as a file run does (review of #2268).
    seen = {}

    async def fake_collect(days):
        seen["days"] = days
        return {"clicks": [], "edges": [], "_errors": {}}

    monkeypatch.setattr(f, "collect", fake_collect)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setattr(f.sys, "argv", ["-c"])
    assert f.main() == 0 and seen["days"] == 30
    monkeypatch.setattr(f.sys, "argv", ["-c", "--days", "7"])
    assert f.main() == 0 and seen["days"] == 7
    assert f.main(["--days", "3"]) == 0 and seen["days"] == 3


def test_an_edge_without_its_purchase_row_is_an_orphan_not_a_mismatch():
    rows = {"clicks": [], "edges": [], "_errors": {}, "purchases": [], "completed_without_edge": [],
            "partner_edge_agent_check": [
                {"edge_id": "e_orphan", "edge_agent": "agent_x", "purchase_agent": "",
                 "purchase_found": False, "purchase_id": "rp_gone", "created_at": None},
                {"edge_id": "e_mm", "edge_agent": "agent_x", "purchase_agent": "agent_y",
                 "purchase_found": True, "purchase_id": "rp_1", "created_at": None},
            ]}
    fn = f.build_funnel(rows, 30, now=NOW)
    assert [r["edge_id"] for r in fn["exceptions"]["orphan_edge"]["rows"]] == ["e_orphan"]
    assert [r["edge_id"] for r in fn["exceptions"]["agent_mismatch"]["rows"]] == ["e_mm"]
    assert "partner edges with no purchase row: 1" in f.render(fn)


def test_money_is_displayed_in_each_currency_s_own_minor_unit():
    assert f._money({"USD": 4500}) == "USD 45.00"
    assert f._money({"JPY": 4500}) == "JPY 4,500"
    assert f._money({"KWD": 4500}) == "KWD 4.500"


def test_a_query_error_makes_the_run_fail(monkeypatch):
    async def fake_collect(days):
        return {"clicks": [], "edges": [], "_errors": {"edges": "boom"}}

    monkeypatch.setattr(f, "collect", fake_collect)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    assert f.main(["--days", "1"]) == 1
