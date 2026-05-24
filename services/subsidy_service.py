from __future__ import annotations

import hashlib
import json
from typing import Any

from db.database import IS_POSTGRES, database


ALLOWED_KINDS = (
    "waived_setup_fee",
    "complimentary_credits",
    "discounted_subscription",
    "co_funded_marketing",
)


class SubsidyCapExceeded(Exception):
    """Raised when issuing a subsidy would push a brand past its cap."""

    def __init__(
        self,
        *,
        cap_cents: int,
        already_issued_cents: int,
        requested_cents: int,
    ) -> None:
        self.cap_cents = cap_cents
        self.already_issued_cents = already_issued_cents
        self.requested_cents = requested_cents
        super().__init__(
            f"Subsidy cap exceeded: cap={cap_cents}, "
            f"already_issued={already_issued_cents}, "
            f"requested={requested_cents}, "
            f"available={cap_cents - already_issued_cents}"
        )


class SubsidyKindInvalid(Exception):
    """Raised when kind is not one of the four brief §7.4 allowed values."""


async def issue(
    *,
    channel_partner_id: int,
    merchant_id: str,
    kind: str,
    amount_cents: int,
    reference_id: str | None = None,
    notes: str | None = None,
    issued_by: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Issue a subsidy event atomically and return the new ledger row id."""

    merchant_id = _validate_inputs(
        channel_partner_id=channel_partner_id,
        merchant_id=merchant_id,
        kind=kind,
        amount_cents=amount_cents,
        metadata=metadata,
    )
    metadata_payload = metadata or {}

    async with database.transaction():
        partner = await database.fetch_one(
            """
            SELECT per_brand_subsidy_cap_cents
            FROM channel_partners
            WHERE id = :id
            FOR UPDATE
            """,
            {"id": channel_partner_id},
        )
        if not partner:
            raise ValueError(f"Channel partner not found: {channel_partner_id}")

        advisory_key = _hash_to_int(f"subsidy:{channel_partner_id}:{merchant_id}")
        await database.execute(
            "SELECT pg_advisory_xact_lock(:k)",
            {"k": advisory_key},
        )

        cap = _row_get(partner, "per_brand_subsidy_cap_cents")
        total_row = await database.fetch_one(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM partner_subsidy_ledger
            WHERE channel_partner_id = :channel_partner_id
              AND merchant_id = :merchant_id
            """,
            {
                "channel_partner_id": channel_partner_id,
                "merchant_id": merchant_id,
            },
        )
        already_issued_cents = int(_row_get(total_row, "total") or 0)

        if cap is not None and already_issued_cents + amount_cents > int(cap):
            raise SubsidyCapExceeded(
                cap_cents=int(cap),
                already_issued_cents=already_issued_cents,
                requested_cents=amount_cents,
            )

        row = await database.fetch_one(
            f"""
            INSERT INTO partner_subsidy_ledger (
              channel_partner_id,
              merchant_id,
              kind,
              amount_cents,
              reference_id,
              notes,
              issued_by,
              metadata
            ) VALUES (
              :channel_partner_id,
              :merchant_id,
              :kind,
              :amount_cents,
              :reference_id,
              :notes,
              :issued_by,
              {_json_param("metadata_json")}
            )
            RETURNING id
            """,
            {
                "channel_partner_id": channel_partner_id,
                "merchant_id": merchant_id,
                "kind": kind,
                "amount_cents": amount_cents,
                "reference_id": reference_id,
                "notes": notes,
                "issued_by": issued_by,
                "metadata_json": json.dumps(metadata_payload, default=str),
            },
        )
        if not row:
            raise RuntimeError("Unable to insert partner subsidy ledger row")
        return int(_row_get(row, "id"))


async def per_brand_subsidy_total_cents(
    *,
    channel_partner_id: int,
    merchant_id: str,
) -> int:
    """Sum of subsidy amounts already issued for a partner and brand."""

    merchant_id = _normalize_merchant_id(merchant_id)
    row = await database.fetch_one(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS total
        FROM partner_subsidy_ledger
        WHERE channel_partner_id = :channel_partner_id
          AND merchant_id = :merchant_id
        """,
        {
            "channel_partner_id": channel_partner_id,
            "merchant_id": merchant_id,
        },
    )
    return int(_row_get(row, "total") or 0)


def _validate_inputs(
    *,
    channel_partner_id: int,
    merchant_id: str,
    kind: str,
    amount_cents: int,
    metadata: dict[str, Any] | None,
) -> str:
    if int(channel_partner_id) <= 0:
        raise ValueError("channel_partner_id must be positive")
    merchant_id = _normalize_merchant_id(merchant_id)
    if kind not in ALLOWED_KINDS:
        allowed = ", ".join(ALLOWED_KINDS)
        raise SubsidyKindInvalid(
            f"Invalid subsidy kind: {kind!r}. Allowed values: {allowed}"
        )
    if int(amount_cents) <= 0:
        raise ValueError("amount_cents must be > 0")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict")
    return merchant_id


def _normalize_merchant_id(merchant_id: str) -> str:
    normalized = str(merchant_id or "").strip()
    if not normalized:
        raise ValueError("merchant_id must be non-empty")
    return normalized


def _hash_to_int(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _json_param(name: str) -> str:
    return f"CAST(:{name} AS jsonb)" if IS_POSTGRES else f":{name}"


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key)
