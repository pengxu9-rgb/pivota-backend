import base64
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from db.database import database
from services.pcs_hash import chain_hash, sha256_hex

logger = logging.getLogger(__name__)


def verify_shopify_hmac(*, secret: str, payload: bytes, header_hmac_base64: Optional[str]) -> bool:
    if not secret or not header_hmac_base64:
        return False
    digest = hmac.new(secret.encode("utf-8"), payload, digestmod="sha256").digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, header_hmac_base64)


def build_idempotency_key(*, shop_domain: str, topic: str, webhook_id: Optional[str], payload_sha256: str) -> str:
    if webhook_id:
        return f"{shop_domain}:{topic}:{webhook_id}"
    return f"{shop_domain}:{topic}:{payload_sha256}"


async def _get_prev_chain_hash(merchant_id: str) -> Optional[str]:
    row = await database.fetch_one(
        """
        SELECT chain_hash
        FROM pcs_shopify_webhook_events
        WHERE merchant_id = :merchant_id
        ORDER BY id DESC
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if not row:
        return None
    return row["chain_hash"]


async def ingest_shopify_webhook(
    *,
    merchant_id: str,
    topic: str,
    payload: bytes,
    shop_domain: str,
    webhook_id: Optional[str],
    occurred_at: Optional[datetime],
    signature_verified: bool,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Persist Shopify webhook into pcs_shopify_webhook_events with idempotency guard.

    Returns:
      (is_duplicate, row_dict)
    """
    payload_sha = sha256_hex(payload)
    idempotency_key = build_idempotency_key(
        shop_domain=shop_domain or "unknown",
        topic=topic or "unknown",
        webhook_id=webhook_id,
        payload_sha256=payload_sha,
    )

    try:
        payload_json = json.loads(payload.decode("utf-8"))
    except Exception:
        payload_json = {"_raw": payload.decode("utf-8", errors="replace")}
    # NOTE: The `databases` + `asyncpg` stack expects JSON/JSONB bind params as `str`,
    # not Python `dict`. Serialize explicitly to avoid runtime 500s (and Shopify retries).
    payload_json_str = json.dumps(payload_json, ensure_ascii=False)

    async with database.transaction():
        # Prevent hash-chain forks by serializing per merchant within the transaction.
        try:
            await database.execute(
                "SELECT pg_advisory_xact_lock(hashtext(:merchant_id))",
                {"merchant_id": merchant_id},
            )
        except Exception:
            # If advisory locks are unavailable, we still persist events but the chain may fork under concurrency.
            logger.debug("pg_advisory_xact_lock unavailable; proceeding without merchant lock")

        prev = await _get_prev_chain_hash(merchant_id)
        occurred_iso = (occurred_at or datetime.utcnow()).isoformat()
        chash = chain_hash(prev, payload_sha, idempotency_key, occurred_iso)

        inserted = await database.fetch_one(
            """
            INSERT INTO pcs_shopify_webhook_events
              (merchant_id, shop_domain, topic, webhook_id, idempotency_key, signature_verified,
               occurred_at, payload_json, payload_sha256, prev_chain_hash, chain_hash)
            VALUES
              (:merchant_id, :shop_domain, :topic, :webhook_id, :idempotency_key, :signature_verified,
               :occurred_at, CAST(:payload_json AS jsonb), :payload_sha256, :prev_chain_hash, :chain_hash)
            ON CONFLICT (merchant_id, idempotency_key)
            DO NOTHING
            RETURNING *
            """,
            {
                "merchant_id": merchant_id,
                "shop_domain": shop_domain,
                "topic": topic,
                "webhook_id": webhook_id,
                "idempotency_key": idempotency_key,
                "signature_verified": signature_verified,
                "occurred_at": occurred_at,
                "payload_json": payload_json_str,
                "payload_sha256": payload_sha,
                "prev_chain_hash": prev,
                "chain_hash": chash,
            },
        )

    if inserted:
        return False, dict(inserted)

    # Duplicate: load existing row for observability.
    existing = await database.fetch_one(
        """
        SELECT *
        FROM pcs_shopify_webhook_events
        WHERE merchant_id = :merchant_id AND idempotency_key = :idempotency_key
        """,
        {"merchant_id": merchant_id, "idempotency_key": idempotency_key},
    )

    return True, dict(existing) if existing else {
        "merchant_id": merchant_id,
        "topic": topic,
        "idempotency_key": idempotency_key,
        "payload_sha256": payload_sha,
    }
