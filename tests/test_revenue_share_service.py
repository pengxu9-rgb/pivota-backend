from decimal import Decimal

import pytest

from services.order_commission_service import OrderCommissionService
from services.revenue_share_service import (
    RevenueShareService,
    VALID_REVENUE_MATCH_SOURCES,
    VALID_REVENUE_MATCH_STATUSES,
    normalize_revenue_match_source,
    normalize_revenue_match_status,
)


def test_no_rules_fallback_matches_revenue_log_schema():
    result = RevenueShareService(database=None).calculate_match(
        merchant_offer=None,
        agent_expectation=None,
        agent_type="basic",
        order_amount=Decimal("4.07"),
    )

    assert result["actual_rate"] == 0.01
    assert result["match_status"] == "no_rules"
    assert result["match_source"] == "platform_default"
    assert result["match_status"] in VALID_REVENUE_MATCH_STATUSES
    assert result["match_source"] in VALID_REVENUE_MATCH_SOURCES


def test_agent_expectation_only_fallback_matches_revenue_log_schema():
    result = RevenueShareService(database=None).calculate_match(
        merchant_offer=None,
        agent_expectation={
            "expected_commission_rate": Decimal("0.10"),
            "min_acceptable_rate": Decimal("0.05"),
        },
        agent_type="basic",
        order_amount=Decimal("4.07"),
    )

    assert result["actual_rate"] == 0.01
    assert result["match_status"] == "fallback_platform"
    assert result["match_source"] == "platform_default"
    assert result["match_status"] in VALID_REVENUE_MATCH_STATUSES
    assert result["match_source"] in VALID_REVENUE_MATCH_SOURCES


def test_below_minimum_merchant_offer_fallback_matches_revenue_log_schema():
    result = RevenueShareService(database=None).calculate_match(
        merchant_offer={"offered_commission_rate": Decimal("0.02")},
        agent_expectation={
            "expected_commission_rate": Decimal("0.10"),
            "min_acceptable_rate": Decimal("0.05"),
        },
        agent_type="basic",
        order_amount=Decimal("4.07"),
    )

    assert result["actual_rate"] == 0.01
    assert result["match_status"] == "agent_below_min"
    assert result["match_source"] == "platform_default"
    assert result["match_status"] in VALID_REVENUE_MATCH_STATUSES
    assert result["match_source"] in VALID_REVENUE_MATCH_SOURCES


def test_revenue_match_normalizers_accept_legacy_service_names():
    assert normalize_revenue_match_status("platform_fallback") == "fallback_platform"
    assert normalize_revenue_match_source("platform_policy") == "platform_default"


class _CaptureDatabase:
    def __init__(self):
        self.values = None

    async def execute(self, query, values):
        self.values = values
        return 1


class _ExistingCommissionDatabase:
    def __init__(self, row):
        self.row = row
        self.query = None
        self.values = None

    async def fetch_one(self, query, values):
        self.query = query
        self.values = values
        return self.row


@pytest.mark.asyncio
async def test_order_commission_log_normalizes_legacy_revenue_match_names():
    database = _CaptureDatabase()
    service = OrderCommissionService(database)

    await service._log_revenue_matching(
        order_id="ORD_TEST",
        order={
            "agent_id": "agent_1",
            "merchant_id": "merchant_1",
            "total": Decimal("4.07"),
            "currency": "EUR",
        },
        match_result={
            "actual_rate": 0.01,
            "match_status": "platform_fallback",
            "match_source": "platform_policy",
            "platform_default_used": True,
        },
        commission_amount=Decimal("0.0407"),
    )

    assert database.values["match_status"] == "fallback_platform"
    assert database.values["match_source"] == "platform_default"


@pytest.mark.asyncio
async def test_existing_commission_check_includes_commissions_table():
    database = _ExistingCommissionDatabase(row={"exists": 1})
    service = OrderCommissionService(database)

    assert await service._check_existing_commission("ORD_TEST") is True
    assert "FROM revenue_matching_logs" in database.query
    assert "FROM commissions" in database.query
    assert database.values == {"order_id": "ORD_TEST"}


@pytest.mark.asyncio
async def test_existing_commission_check_returns_false_when_no_audit_or_commission_row():
    database = _ExistingCommissionDatabase(row=None)
    service = OrderCommissionService(database)

    assert await service._check_existing_commission("ORD_TEST") is False
