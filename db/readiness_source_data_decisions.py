from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from db.database import database


_TABLE_NAME = "merchant_readiness_source_data_decisions"


def _decision_key(platform: Optional[str], platform_product_id: Optional[str]) -> str:
    return f"{str(platform or '').strip().lower()}|{str(platform_product_id or '').strip()}"


async def ensure_source_data_decisions_table() -> None:
    await database.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
            merchant_id VARCHAR(50) NOT NULL,
            reason_code VARCHAR(64) NOT NULL,
            platform VARCHAR(50) NOT NULL,
            platform_product_id VARCHAR(100) NOT NULL,
            decision_state VARCHAR(64) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (merchant_id, reason_code, platform, platform_product_id)
        )
        """
    )
    await database.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{_TABLE_NAME}_merchant_reason
        ON {_TABLE_NAME} (merchant_id, reason_code)
        """
    )


async def list_source_data_decisions(
    merchant_id: str,
    *,
    reason_code: Optional[str] = None,
    product_keys: Optional[Iterable[tuple[str, str]]] = None,
) -> Dict[str, Dict[str, Any]]:
    await ensure_source_data_decisions_table()

    clauses = ["merchant_id = :merchant_id"]
    values: Dict[str, Any] = {"merchant_id": merchant_id}

    if reason_code:
        clauses.append("reason_code = :reason_code")
        values["reason_code"] = reason_code

    normalized_keys = [
        (str(platform or "").strip().lower(), str(platform_product_id or "").strip())
        for platform, platform_product_id in (product_keys or [])
        if str(platform_product_id or "").strip()
    ]
    if normalized_keys:
        pair_clauses: list[str] = []
        for index, (platform, platform_product_id) in enumerate(normalized_keys):
            pair_clauses.append(
                f"(platform = :platform_{index} AND platform_product_id = :platform_product_id_{index})"
            )
            values[f"platform_{index}"] = platform
            values[f"platform_product_id_{index}"] = platform_product_id
        clauses.append(f"({' OR '.join(pair_clauses)})")

    query = f"""
        SELECT merchant_id, reason_code, platform, platform_product_id, decision_state, created_at, updated_at
        FROM {_TABLE_NAME}
        WHERE {' AND '.join(clauses)}
    """
    rows = await database.fetch_all(query, values)
    return {_decision_key(row["platform"], row["platform_product_id"]): dict(row) for row in rows}


async def upsert_source_data_decision(
    merchant_id: str,
    *,
    reason_code: str,
    platform: str,
    platform_product_id: str,
    decision_state: str,
) -> Dict[str, Any]:
    await ensure_source_data_decisions_table()
    normalized_platform = str(platform or "").strip().lower()
    normalized_product_id = str(platform_product_id or "").strip()
    await database.execute(
        f"""
        INSERT INTO {_TABLE_NAME} (
            merchant_id,
            reason_code,
            platform,
            platform_product_id,
            decision_state
        ) VALUES (
            :merchant_id,
            :reason_code,
            :platform,
            :platform_product_id,
            :decision_state
        )
        ON CONFLICT (merchant_id, reason_code, platform, platform_product_id)
        DO UPDATE SET
            decision_state = EXCLUDED.decision_state,
            updated_at = CURRENT_TIMESTAMP
        """,
        {
            "merchant_id": merchant_id,
            "reason_code": reason_code,
            "platform": normalized_platform,
            "platform_product_id": normalized_product_id,
            "decision_state": decision_state,
        },
    )
    decisions = await list_source_data_decisions(
        merchant_id,
        reason_code=reason_code,
        product_keys=[(normalized_platform, normalized_product_id)],
    )
    return decisions.get(_decision_key(normalized_platform, normalized_product_id), {})


async def delete_source_data_decision(
    merchant_id: str,
    *,
    reason_code: str,
    platform: str,
    platform_product_id: str,
) -> bool:
    await ensure_source_data_decisions_table()
    normalized_platform = str(platform or "").strip().lower()
    normalized_product_id = str(platform_product_id or "").strip()
    query = f"""
        DELETE FROM {_TABLE_NAME}
        WHERE merchant_id = :merchant_id
          AND reason_code = :reason_code
          AND platform = :platform
          AND platform_product_id = :platform_product_id
    """
    result = await database.execute(
        query,
        {
            "merchant_id": merchant_id,
            "reason_code": reason_code,
            "platform": normalized_platform,
            "platform_product_id": normalized_product_id,
        },
    )
    try:
        return int(result or 0) > 0
    except Exception:
        return True

