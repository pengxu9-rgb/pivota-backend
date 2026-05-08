"""
Phase D wire-up — accessors for the gsc_oauth_tokens table.

Encryption mirrors the connector_credentials pattern: refresh_token
+ access_token are stored as base64(AES-GCM(json)) blobs via
services.crypto_service. The plaintext never touches the DB layer.

Schema is in db/migrations/074_gsc_integration.sql.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def upsert_oauth_tokens(
    *,
    merchant_id: str,
    refresh_token: str,
    access_token: Optional[str],
    access_token_expires_at: Optional[datetime],
    granted_scopes: List[str],
    authorized_site_url: str,
) -> None:
    """Insert or update the per-merchant OAuth row. Encrypts both
    tokens before write. Called from the OAuth callback handler
    after a successful Google authorization-code exchange."""
    from services.crypto_service import crypto_service
    from db.database import database

    refresh_enc = crypto_service.encrypt_json_secret({"v": refresh_token})
    access_enc = (
        crypto_service.encrypt_json_secret({"v": access_token})
        if access_token else None
    )
    await database.execute(
        """
        INSERT INTO gsc_oauth_tokens (
          merchant_id, refresh_token_enc, access_token_enc,
          access_token_expires_at, granted_scopes, authorized_site_url,
          last_refresh_ok_at, last_refresh_error, revoked_at, granted_at
        )
        VALUES (
          :merchant_id, :refresh_enc, :access_enc, :access_expires,
          :scopes, :site_url, NOW(), NULL, NULL, NOW()
        )
        ON CONFLICT (merchant_id) DO UPDATE SET
          refresh_token_enc = EXCLUDED.refresh_token_enc,
          access_token_enc = EXCLUDED.access_token_enc,
          access_token_expires_at = EXCLUDED.access_token_expires_at,
          granted_scopes = EXCLUDED.granted_scopes,
          authorized_site_url = EXCLUDED.authorized_site_url,
          last_refresh_ok_at = NOW(),
          last_refresh_error = NULL,
          revoked_at = NULL
        """,
        {
            "merchant_id": merchant_id,
            "refresh_enc": refresh_enc,
            "access_enc": access_enc,
            "access_expires": access_token_expires_at,
            "scopes": list(granted_scopes),
            "site_url": authorized_site_url,
        },
    )


async def get_oauth_tokens(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Fetch and decrypt the per-merchant OAuth row. Returns None
    when the merchant hasn't granted access yet, or has revoked.
    Decryption failures are logged + treated as "no tokens" so a
    corrupted row doesn't crash the audit pipeline.

    Output (when present):
      {
        refresh_token: str,
        access_token: str | None,
        access_token_expires_at: datetime | None,
        granted_scopes: List[str],
        authorized_site_url: str,
        last_refresh_ok_at: datetime | None,
      }
    """
    from services.crypto_service import crypto_service
    from db.database import database

    row = await database.fetch_one(
        """
        SELECT refresh_token_enc, access_token_enc,
               access_token_expires_at, granted_scopes,
               authorized_site_url, last_refresh_ok_at
          FROM gsc_oauth_tokens
         WHERE merchant_id = :merchant_id
           AND revoked_at IS NULL
         LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if row is None:
        return None
    try:
        refresh = crypto_service.decrypt_json_secret(row["refresh_token_enc"]).get("v")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "gsc_tokens decrypt failed for merchant=%s: %s", merchant_id, exc,
        )
        return None
    access: Optional[str] = None
    if row["access_token_enc"]:
        try:
            access = crypto_service.decrypt_json_secret(row["access_token_enc"]).get("v")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gsc_tokens access_token decrypt failed for merchant=%s: %s",
                merchant_id, exc,
            )
            access = None
    return {
        "refresh_token": refresh,
        "access_token": access,
        "access_token_expires_at": row["access_token_expires_at"],
        "granted_scopes": list(row["granted_scopes"] or []),
        "authorized_site_url": row["authorized_site_url"],
        "last_refresh_ok_at": row["last_refresh_ok_at"],
    }


async def update_access_token(
    *,
    merchant_id: str,
    access_token: str,
    access_token_expires_at: datetime,
) -> None:
    """Called after a successful refresh-token exchange. Updates
    only the access_token + expiry; refresh_token + scopes
    untouched."""
    from services.crypto_service import crypto_service
    from db.database import database

    enc = crypto_service.encrypt_json_secret({"v": access_token})
    await database.execute(
        """
        UPDATE gsc_oauth_tokens
           SET access_token_enc = :enc,
               access_token_expires_at = :expires_at,
               last_refresh_ok_at = NOW(),
               last_refresh_error = NULL
         WHERE merchant_id = :merchant_id
        """,
        {
            "enc": enc,
            "expires_at": access_token_expires_at,
            "merchant_id": merchant_id,
        },
    )


async def record_refresh_error(merchant_id: str, error_message: str) -> None:
    """Records a failed refresh — usually means the refresh_token
    has been revoked at Google's end (user disconnected via Search
    Console UI, or the project's OAuth client was deleted). Audit
    pipeline reads last_refresh_error to surface the disconnect."""
    from db.database import database

    await database.execute(
        """
        UPDATE gsc_oauth_tokens
           SET last_refresh_error = :err
         WHERE merchant_id = :merchant_id
        """,
        {"err": error_message, "merchant_id": merchant_id},
    )


async def mark_revoked(merchant_id: str) -> None:
    """Soft-delete: set revoked_at. The row stays for audit/history;
    audit pipeline treats revoked tokens as "no tokens"."""
    from db.database import database

    await database.execute(
        """
        UPDATE gsc_oauth_tokens
           SET revoked_at = NOW()
         WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
