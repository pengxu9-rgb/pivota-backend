from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, String, Table
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


# One row per issued browser collector token (universal web collector or
# Shopify web pixel). The JWT stays the credential; this row is what lets a
# merchant see, revoke, and renew it. Rows are never deleted: a revoked or
# superseded token keeps its history.
merchant_collector_tokens = Table(
    "merchant_collector_tokens",
    metadata,
    Column("jti", String(64), primary_key=True),
    Column("merchant_id", String(50), nullable=False),
    Column("store_id", String(128), nullable=False),
    Column("token_type", String(32), nullable=False),
    # Format version of the JWT (`v` claim) and the store's token generation
    # at issuance (`sv` claim). A store-wide revocation bumps the generation.
    Column("token_version", Integer, nullable=False),
    Column("store_token_version", Integer, nullable=False),
    Column("allowed_origins", JSONB_TYPE, nullable=True),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("revoked_reason", String(64), nullable=True),
    Column("superseded_by", String(64), nullable=True),
    Column("issued_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index(
    "idx_merchant_collector_tokens_store",
    merchant_collector_tokens.c.merchant_id,
    merchant_collector_tokens.c.store_id,
)
# The renewal sweep asks "what expires soon and is still live".
Index(
    "idx_merchant_collector_tokens_expiring",
    merchant_collector_tokens.c.expires_at,
    postgresql_where=merchant_collector_tokens.c.revoked_at.is_(None),
    sqlite_where=merchant_collector_tokens.c.revoked_at.is_(None),
)


# Per-store token generation. Tokens whose `sv` claim is below
# `min_token_version` are refused whether or not their row exists, which is
# how tokens issued before the registry (no `jti`) are revoked as a set.
merchant_collector_token_policy = Table(
    "merchant_collector_token_policy",
    metadata,
    Column("store_id", String(128), primary_key=True),
    Column("merchant_id", String(50), nullable=False),
    Column("min_token_version", Integer, nullable=False, server_default="1"),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)
