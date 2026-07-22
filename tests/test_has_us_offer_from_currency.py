"""`has_us_offer` must come from CURRENCY, not catalog_offers.market.

`market` is a NOT NULL DEFAULT 'US' (migration 149, "refine to real per-offer geo
when modeled") that the external-seed offer writers never set — so it is 'US' on
every row and carries no signal. Deriving the US-buyable gate from it meant a
GBP/INR/ZAR-only listing reported has_us_offer=TRUE and was served to US shoppers
as a purchase option. currency is written truthfully by every writer.
"""
import services.index_pipeline_state_service as ips


def test_has_us_offer_is_derived_from_currency_not_market():
    """The eligibility SQL must key on currency; market must not gate it."""
    sql = ips._ELIGIBILITY_COLUMNS
    marker = sql[sql.index("AS has_us_offer") - 700: sql.index("AS has_us_offer")]
    assert "upper(coalesce(co.currency, '')) = 'USD'" in marker
    assert "co.market = 'US'" not in marker


def test_sqlite_path_also_derives_from_currency():
    """The test path must exercise the same predicate, not a placeholder.

    It previously hardcoded `0 AS has_us_offer`, which made the gate unreachable
    in every SQLite-backed test.
    """
    sql = ips._SINGLE_QUERY_SQLITE
    assert "0 AS has_us_offer" not in sql
    marker = sql[sql.index("AS has_us_offer") - 400: sql.index("AS has_us_offer")]
    assert "upper(coalesce(co.currency, '')) = 'USD'" in marker


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
