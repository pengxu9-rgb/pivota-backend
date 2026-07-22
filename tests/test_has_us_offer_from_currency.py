"""`has_us_offer` must come from CURRENCY, not catalog_offers.market.

`market` is a NOT NULL DEFAULT 'US' (migration 149: "refine to real per-offer geo
when modeled") that the external-seed offer writers never set — so it is 'US' on
every row and carries no signal. Deriving the US-buyable gate from it meant a
GBP/INR/ZAR-only listing reported has_us_offer=TRUE and was served to US shoppers
as a purchase option. currency is written truthfully by every writer.
"""
import pytest

import services.index_pipeline_state_service as ips

CURRENCY_PREDICATE = "upper(trim(coalesce(co.currency, ''))) = 'USD'"


def _has_us_offer_clause(sql: str) -> str:
    """The subquery that defines has_us_offer, isolated without fixed-width slicing."""
    end = sql.index("AS has_us_offer")
    # walk back to the start of this select-item (the previous column's comma)
    return sql[sql.rindex(",", 0, sql.rindex("EXISTS", 0, end)) : end]


@pytest.mark.parametrize("const_name", ["_ELIGIBILITY_COLUMNS", "_SINGLE_QUERY_SQLITE"])
def test_has_us_offer_derives_from_currency_on_every_path(const_name):
    """Both the Postgres and the SQLite path must key on currency, never market."""
    clause = _has_us_offer_clause(getattr(ips, const_name))
    assert CURRENCY_PREDICATE in clause
    assert "market" not in clause


def test_sqlite_path_is_not_a_placeholder():
    """It used to be `0 AS has_us_offer`, which made the gate unreachable in tests."""
    assert "0 AS has_us_offer" not in ips._SINGLE_QUERY_SQLITE


def test_us_offer_gate_blocks_foreign_only_row_when_flag_on(monkeypatch):
    """End-to-end through the EXISTING agent-decision gate (no new flag)."""
    import services.agent_decision_gates as gates

    monkeypatch.setattr(gates, "agent_decision_gates_enabled", lambda: True)
    monkeypatch.setattr(ips, "agent_decision_gates_enabled", lambda: True)
    monkeypatch.setattr(gates, "evidence_gates_enabled", lambda: False)

    row = {
        "content_key": "ck_x", "sync_status": "live", "pdp_sync_status": "live",
        "pdp_title": "Essence", "seed_title": "Essence", "pdp_description": "x" * 200,
        "image_url": "https://cdn/x.jpg", "content_quality_score": 90.0,
        "has_price": True, "has_us_offer": False,          # priced, but not in USD
        "identity_status": "approved", "identity_confidence": 0.95,
        "product_group_id": "pg_test",                     # satisfies identity_resolved
    }
    out = ips._classify_product(row, set())
    assert out["serving_eligible"] is False
    assert out["blocker_code"] == gates.BLOCKER_NO_US_OFFER

    row["has_us_offer"] = True
    assert ips._classify_product(row, set())["blocker_code"] != gates.BLOCKER_NO_US_OFFER
