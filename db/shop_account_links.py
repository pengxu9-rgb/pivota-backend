"""Storage for a buyer's connected Shop account and the checkouts Pivota runs through Shop MCP.

Design: the local Shop MCP sign-in and checkout design (2026-11-06), sections D2, D4, D5 and D7.
NOTHING READS OR WRITES THESE TABLES YET. The connect flow, the token refresher and the
checkout tools land behind their own flags, all default off.

Four tables:

  shop_account_claims  one row per Shop account (`shop_sub_hash`) that is connected anywhere,
                       naming the ONE Pivota account it belongs to (D2.5). Its primary key, and
                       every live link's foreign key to (sub, account), make "never on two Pivota
                       accounts" a constraint, not a convention: a live link cannot exist without
                       its account's claim, and a claim cannot be deleted under a live link.
  shop_account_links   one row per (Pivota account, agent client) connection. Holds the Shop
                       refresh token ENCRYPTED with `utils.token_cipher` (never plaintext, never
                       the access token). At most one live row per (account, client).
  shop_link_attempts   one row per Connect click: the hashed link ticket and OAuth `state`, and
                       the PKCE verifier encrypted. Single use, short-lived.
  shop_checkouts       one row per Shop MCP checkout Pivota created. Keyed by a HASH of Shop's
                       checkout id, because the raw id and `continue_url` carry an access key and
                       must never be stored. `completion_attempted_at` is set, by an UPDATE
                       guarded with `completion_attempted_at IS NULL`, before the one and only
                       `complete_checkout` call (D7). The writer never clears it; a CHECK cannot
                       stop an UPDATE to NULL, so that rule lives in the writer and its test.

No table references an existing table, so creating them locks nothing that serves traffic.

NOT REGISTERED IN main.py YET, on purpose. Prod does not apply numbered migrations; it gets tables
from `metadata.create_all` at boot, which never ALTERs an existing table. Registering now would
freeze this shape in prod before its first writer exists, and every change after that would need a
hand-applied ALTER. The first writer PR adds `import db.shop_account_links` to main.py. Until then
these tables exist only where migrations run (CI, the dialect gate, local), from
db/migrations/277_shop_account_links.sql.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata

LINK_STATUSES = ("active", "needs_reconnect", "revoked")
CHECKOUT_OUTCOMES = ("open", "handed_off", "completed", "canceled", "declined", "unknown_outcome")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def sha256_hex(value: str) -> str:
    """The one hash every `*_hash` column here stores (lower-case hex sha256 of the UTF-8 text)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def refresh_token_aad(*, link_id: str, pivota_account_id: str, agent_client_id: str) -> str:
    """The `utils.token_cipher` context a link's refresh token is sealed under. It names the row AND
    its owner, so a ciphertext moved to another row, or a row re-pointed at another account or
    client in the database, no longer decrypts."""
    return f"shop_account_links.refresh_token:{link_id}:{pivota_account_id}:{agent_client_id}"


def code_verifier_aad(*, attempt_id: str, pivota_account_id: str, agent_client_id: str) -> str:
    return f"shop_link_attempts.code_verifier:{attempt_id}:{pivota_account_id}:{agent_client_id}"


def _hash_len(column: str, table: str) -> CheckConstraint:
    # NULL passes a CHECK, so a nullable hash column needs no IS NULL arm.
    return CheckConstraint(f"length({column}) = 64", name=f"ck_{table}_{column}_len")


shop_account_claims = Table(
    "shop_account_claims",
    metadata,
    Column("shop_sub_hash", String(64), primary_key=True),
    Column("pivota_account_id", String(128), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    _hash_len("shop_sub_hash", "shop_account_claims"),
    # The target of every live link's foreign key: (sub, the account that holds it).
    UniqueConstraint("shop_sub_hash", "pivota_account_id", name="uq_shop_account_claims_sub_account"),
)
Index("ix_shop_account_claims_account", shop_account_claims.c.pivota_account_id)


shop_account_links = Table(
    "shop_account_links",
    metadata,
    Column("link_id", String(64), primary_key=True),
    Column("pivota_account_id", String(128), nullable=False),
    Column("agent_client_id", String(128), nullable=False),
    Column("shop_sub_hash", String(64), nullable=False),
    # Equals shop_sub_hash while the link is live, NULL once revoked. It carries the foreign key to
    # the claim, so a revoked tombstone never blocks the claim's release (MATCH SIMPLE skips NULL).
    Column("claimed_sub_hash", String(64), nullable=True),
    Column("scope", String(512), nullable=False),
    # `tc1.<kid>.<body>` from utils.token_cipher, sealed under `refresh_token_aad(...)`. NULL once
    # revoked. The key id is read from the envelope; it is not stored twice.
    Column("refresh_token_ct", Text, nullable=True),
    Column("link_generation", Integer, nullable=False, server_default=text("1")),
    Column("refresh_lease_until", DateTime(timezone=True), nullable=True),
    Column("status", String(16), nullable=False),
    Column("status_reason", String(64), nullable=True),
    Column("connected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_refreshed_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_in("status", LINK_STATUSES), name="ck_shop_account_links_status"),
    # An active link has a token to use; a revoked one has none left to leak.
    CheckConstraint(
        "(status <> 'active') OR (refresh_token_ct IS NOT NULL)",
        name="ck_shop_account_links_active_has_token",
    ),
    CheckConstraint(
        "(status = 'revoked') = (revoked_at IS NOT NULL)",
        name="ck_shop_account_links_revoked_iff_revoked_at",
    ),
    CheckConstraint(
        "(status <> 'revoked') OR (refresh_token_ct IS NULL)",
        name="ck_shop_account_links_revoked_has_no_token",
    ),
    # Written so it can never evaluate to NULL (a NULL CHECK passes): the live arm tests
    # claimed_sub_hash IS NOT NULL before comparing it.
    CheckConstraint(
        "(status = 'revoked' AND claimed_sub_hash IS NULL)"
        " OR (status <> 'revoked' AND claimed_sub_hash IS NOT NULL AND claimed_sub_hash = shop_sub_hash)",
        name="ck_shop_account_links_live_is_claimed",
    ),
    CheckConstraint("link_generation >= 1", name="ck_shop_account_links_generation_positive"),
    _hash_len("shop_sub_hash", "shop_account_links"),
    ForeignKeyConstraint(
        ["claimed_sub_hash", "pivota_account_id"],
        ["shop_account_claims.shop_sub_hash", "shop_account_claims.pivota_account_id"],
        name="fk_shop_account_links_claim",
    ),
)

# D8 / O5: one live connection per (account, agent client).
Index(
    "uq_shop_account_links_active_account_client",
    shop_account_links.c.pivota_account_id,
    shop_account_links.c.agent_client_id,
    unique=True,
    postgresql_where=text("status <> 'revoked'"),
    sqlite_where=text("status <> 'revoked'"),
)
# Lookup for D2.5's claim release: the claim goes when the sub's last live link does.
Index(
    "ix_shop_account_links_live_sub",
    shop_account_links.c.shop_sub_hash,
    postgresql_where=text("status <> 'revoked'"),
    sqlite_where=text("status <> 'revoked'"),
)


shop_link_attempts = Table(
    "shop_link_attempts",
    metadata,
    Column("attempt_id", String(64), primary_key=True),
    Column("ticket_hash", String(64), nullable=False),
    Column("state_hash", String(64), nullable=True),
    Column("pivota_account_id", String(128), nullable=False),
    Column("agent_client_id", String(128), nullable=False),
    # Sealed under `code_verifier_aad(...)`; set at Connect, with state.
    Column("code_verifier_ct", Text, nullable=True),
    Column("nonce_hash", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("connect_clicked_at", DateTime(timezone=True), nullable=True),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    Column("outcome", String(32), nullable=True),
    # Connect sets state, verifier and nonce together, or none of them.
    CheckConstraint(
        "((state_hash IS NULL) = (code_verifier_ct IS NULL)) AND ((state_hash IS NULL) = (nonce_hash IS NULL))",
        name="ck_shop_link_attempts_connect_fields_together",
    ),
    CheckConstraint(
        "(state_hash IS NULL) = (connect_clicked_at IS NULL)",
        name="ck_shop_link_attempts_state_iff_clicked",
    ),
    CheckConstraint("expires_at > created_at", name="ck_shop_link_attempts_expiry_after_creation"),
    _hash_len("ticket_hash", "shop_link_attempts"),
    _hash_len("state_hash", "shop_link_attempts"),
    _hash_len("nonce_hash", "shop_link_attempts"),
)
Index("uq_shop_link_attempts_ticket_hash", shop_link_attempts.c.ticket_hash, unique=True)
Index(
    "uq_shop_link_attempts_state_hash",
    shop_link_attempts.c.state_hash,
    unique=True,
    postgresql_where=text("state_hash IS NOT NULL"),
    sqlite_where=text("state_hash IS NOT NULL"),
)
Index("ix_shop_link_attempts_expires_at", shop_link_attempts.c.expires_at)


shop_checkouts = Table(
    "shop_checkouts",
    metadata,
    Column("checkout_id_hash", String(64), primary_key=True),
    Column("pivota_account_id", String(128), nullable=False),
    Column("agent_client_id", String(128), nullable=False),
    Column("link_id", String(64), nullable=True),
    Column("shop_domain", String(255), nullable=False),
    Column("market", String(8), nullable=False),
    Column("click_id", String(64), nullable=True),
    # What the agent front showed, from get_checkout: totals, currency, line ids. Never an
    # address, an email, `continue_url` or the raw checkout id.
    Column("shown_totals", JSONB_TYPE, nullable=True),
    Column("contact_consent_at", DateTime(timezone=True), nullable=True),
    Column("mandate_hash", String(64), nullable=True),
    Column("completion_attempted_at", DateTime(timezone=True), nullable=True),
    Column("outcome", String(24), nullable=False, server_default=text("'open'")),
    Column("order_ref_hash", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_in("outcome", CHECKOUT_OUTCOMES), name="ck_shop_checkouts_outcome"),
    _hash_len("checkout_id_hash", "shop_checkouts"),
    _hash_len("mandate_hash", "shop_checkouts"),
    _hash_len("order_ref_hash", "shop_checkouts"),
    # D7: a completion is attempted only under a confirmed mandate.
    CheckConstraint(
        "(completion_attempted_at IS NULL) OR (mandate_hash IS NOT NULL)",
        name="ck_shop_checkouts_completion_needs_mandate",
    ),
    # A decline or an unknown outcome exists only after our completion attempt. A completed
    # checkout (ours, or the buyer paying on continue_url) always names its order.
    CheckConstraint(
        "(outcome NOT IN ('declined', 'unknown_outcome')) OR (completion_attempted_at IS NOT NULL)",
        name="ck_shop_checkouts_attempt_outcome_needs_attempt",
    ),
    CheckConstraint(
        "(outcome <> 'completed') OR (order_ref_hash IS NOT NULL)",
        name="ck_shop_checkouts_completed_names_order",
    ),
)
Index("ix_shop_checkouts_account_created", shop_checkouts.c.pivota_account_id, shop_checkouts.c.created_at)
