from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from db.database import database

GmvChannel = Literal["personal_agent", "third_party_agent"]


_RECORD_CLASSIFICATION_QUERY = """
UPDATE commerce_attribution_edges
SET
  gmv_channel = :gmv_channel,
  third_party_platform = :third_party_platform,
  third_party_platform_fee_pct = :third_party_platform_fee_pct,
  updated_at = NOW()
WHERE edge_id = :edge_id
RETURNING edge_id
"""


async def record_classification(
    *,
    edge_id: str,
    gmv_channel: GmvChannel,
    third_party_platform: str | None,
    third_party_platform_fee_pct: Decimal | None,
) -> None:
    """Set GMV classification fields on an existing attribution edge.

    This records classification only. It does not compute or write any dollar
    amounts, partner shares, or backfill guesses.
    """

    if not edge_id:
        raise ValueError("edge_id is required")

    platform_value: str | None
    fee_value: Decimal | None
    if gmv_channel == "personal_agent":
        if third_party_platform is not None or third_party_platform_fee_pct is not None:
            raise ValueError("personal_agent classification must not include third-party platform data")
        platform_value = None
        fee_value = None
    elif gmv_channel == "third_party_agent":
        platform_value = str(third_party_platform or "").strip()
        if not platform_value:
            raise ValueError("third_party_agent classification requires a non-empty third_party_platform")
        fee_value = _coerce_fee_pct(third_party_platform_fee_pct)
        if fee_value < Decimal("0") or fee_value > Decimal("1"):
            raise ValueError("third_party_platform_fee_pct must be between 0 and 1 inclusive")
    else:
        raise ValueError(f"Unsupported gmv_channel: {gmv_channel}")

    row = await database.fetch_one(
        _RECORD_CLASSIFICATION_QUERY,
        {
            "edge_id": edge_id,
            "gmv_channel": gmv_channel,
            "third_party_platform": platform_value,
            "third_party_platform_fee_pct": fee_value,
        },
    )
    if not row:
        raise ValueError(f"Attribution edge not found: {edge_id}")


def _coerce_fee_pct(value: Decimal | None) -> Decimal:
    if value is None:
        raise ValueError("third_party_agent classification requires third_party_platform_fee_pct")
    try:
        fee = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("third_party_platform_fee_pct must be a decimal fraction") from None
    if fee.is_nan() or fee.is_infinite():
        raise ValueError("third_party_platform_fee_pct must be a finite decimal fraction")
    return fee
