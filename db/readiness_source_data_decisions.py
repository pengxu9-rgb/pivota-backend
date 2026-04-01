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
    decisions_by_reason = await list_source_data_decisions_by_reason_codes(
        merchant_id,
        reason_codes=[reason_code] if reason_code else None,
        product_keys=product_keys,
    )
    if reason_code:
        return decisions_by_reason.get(str(reason_code or "").strip(), {})

    flattened: Dict[str, Dict[str, Any]] = {}
    for decisions in decisions_by_reason.values():
        flattened.update(decisions)
    return flattened


async def list_source_data_decisions_by_reason_codes(
    merchant_id: str,
    *,
    reason_codes: Optional[Iterable[str]] = None,
    product_keys: Optional[Iterable[tuple[str, str]]] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    await ensure_source_data_decisions_table()

    clauses = ["merchant_id = :merchant_id"]
    values: Dict[str, Any] = {"merchant_id": merchant_id}

    normalized_reason_codes = [
        str(code or "").strip()
        for code in (reason_codes or [])
        if str(code or "").strip()
    ]
    if normalized_reason_codes:
        reason_clauses: list[str] = []
        for index, code in enumerate(normalized_reason_codes):
            key = f"reason_code_{index}"
            reason_clauses.append(f"reason_code = :{key}")
            values[key] = code
        clauses.append(f"({' OR '.join(reason_clauses)})")

    normalized_keys = [
        (str(platform or "").strip().lower(), str(platform_product_id or "").strip())
        for platform, platform_product_id in (product_keys or [])
        if str(platform_product_id or "").strip()
    ]
    normalized_key_set = set(normalized_keys)
    if normalized_keys:
        normalized_platforms = sorted({platform for platform, _ in normalized_keys if platform})
        normalized_product_ids = sorted(
            {platform_product_id for _, platform_product_id in normalized_keys if platform_product_id}
        )
        if normalized_platforms:
            platform_clauses: list[str] = []
            for index, platform in enumerate(normalized_platforms):
                key = f"platform_{index}"
                platform_clauses.append(f"platform = :{key}")
                values[key] = platform
            clauses.append(f"({' OR '.join(platform_clauses)})")
        if normalized_product_ids:
            product_id_clauses: list[str] = []
            for index, platform_product_id in enumerate(normalized_product_ids):
                key = f"platform_product_id_{index}"
                product_id_clauses.append(f"platform_product_id = :{key}")
                values[key] = platform_product_id
            clauses.append(f"({' OR '.join(product_id_clauses)})")

    query = f"""
        SELECT merchant_id, reason_code, platform, platform_product_id, decision_state, created_at, updated_at
        FROM {_TABLE_NAME}
        WHERE {' AND '.join(clauses)}
    """
    rows = await database.fetch_all(query, values)
    decisions_by_reason: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        platform = str(row["platform"] or "").strip().lower()
        platform_product_id = str(row["platform_product_id"] or "").strip()
        key_tuple = (platform, platform_product_id)
        if normalized_key_set and key_tuple not in normalized_key_set:
            continue
        reason = str(row["reason_code"] or "").strip()
        decisions_by_reason.setdefault(reason, {})[_decision_key(platform, platform_product_id)] = dict(row)
    return decisions_by_reason


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
    key = _decision_key(normalized_platform, normalized_product_id)
    existing = await list_source_data_decisions(
        merchant_id,
        reason_code=reason_code,
        product_keys=[(normalized_platform, normalized_product_id)],
    )
    if key not in existing:
        return False
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
        if result is not None:
            return int(result or 0) > 0
    except Exception:
        pass

    remaining = await list_source_data_decisions(
        merchant_id,
        reason_code=reason_code,
        product_keys=[(normalized_platform, normalized_product_id)],
    )
    return key not in remaining
