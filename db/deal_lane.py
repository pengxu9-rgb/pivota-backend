"""The deal lane's storage: observed store offers and what the checkout said about them (ADR-027).

A deal row is what a source PUBLISHED (a code, its terms, its dates). It is metadata, never evidence.
Evidence is a verification row: the merchant's own checkout applying the deal to a one-unit reference
cart of ONE variant in the deal's market (D3). Serving reads verification rows only, and only while
they are `checkout_verified` and unexpired; everything else is not emitted.

Not a promotions engine: ADR-022 deleted that lane, and `TestPromotionsLaneStaysDeleted` keeps its
modules and routes gone. Nothing here computes or enforces a price; the merchant's checkout does.

Nothing reads or writes these tables yet. Each reader and writer lands behind its own flag
(config/settings.py, "Deal lane"), all default off.

Created by `metadata.create_all` at startup (main.py imports this module), and by
db/migrations/275_deal_lane_tables.sql. The click row's two deal-lane columns are NOT here: they
belong to surface_click_events and arrive by hand-applied migration 276.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    false,
    text,
)
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata

DEAL_KINDS = ("percent", "amount", "free_shipping", "gift", "bxgy")
# D2: the only sources a deal may come from. Coupon aggregators are not one of them (D1).
DEAL_SOURCES = ("checkout_automatic", "merchant_published", "authorized_feed", "channel_exclusive")
VERIFICATION_STATES = ("unverified", "checkout_verified", "failed", "expired")
VERIFIED_VIA = ("shopify_storefront_connected", "shopify_storefront_tokenless", "ucp_checkout")
STOP_TRIGGERS = ("rate", "absolute", "manual")


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# One row per (seller, market, code or automatic rule, program). An open code listed by two programs is
# two rows (Q13, Q14): each program's permission covers only its own feed's row.
observed_store_offers = Table(
    "observed_store_offers",
    metadata,
    Column("deal_id", String(64), primary_key=True),
    # observed_store_offer_key() of the identity columns: the one uniqueness rule, because a UNIQUE over
    # nullable columns lets NULLs repeat.
    Column("deal_key", String(64), nullable=False),
    Column("seller_id", String(128), nullable=False),
    # Never defaulted. A merchant_published row with no market evidence has none, and is never verified
    # or served (D6 "Market").
    Column("market", String(8), nullable=True),
    Column("code", String(255), nullable=True),
    # The platform's own id for an automatic discount where observable, otherwise the allocation title
    # the reference cart showed.
    Column("platform_rule_ref", String(255), nullable=True),
    Column("program_id", String(128), nullable=True),
    Column("kind", String(16), nullable=False),
    Column("value", Numeric(18, 4), nullable=True),
    # The market's currency from the evidence: never the shop default, never FX-converted (ADR-024).
    Column("currency", String(3), nullable=True),
    Column("min_subtotal", Numeric(18, 4), nullable=True),
    # D3 eligibility. Any limited (or unknown) deal is never emitted in v1.
    Column("new_customer_only", Boolean, nullable=False, server_default=false()),
    Column("once_per_customer", Boolean, nullable=False, server_default=false()),
    Column("segment_limited", Boolean, nullable=False, server_default=false()),
    Column("usage_limited", Boolean, nullable=False, server_default=false()),
    Column("eligibility_known", Boolean, nullable=False, server_default=false()),
    # Published scope: chooses which variants to test, never which count as verified.
    Column("scope_kind", String(16), nullable=True),
    Column("scope_refs", JSONB_TYPE, nullable=True),
    Column("starts_at", DateTime(timezone=True), nullable=True),
    Column("ends_at", DateTime(timezone=True), nullable=True),
    Column("source", String(32), nullable=False),
    Column("source_url", Text, nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("terms_text", Text, nullable=True),
    # D7 outcome, from reconciled order evidence.
    Column("orders_seen", Integer, nullable=False, server_default=text("0")),
    Column("orders_applied", Integer, nullable=False, server_default=text("0")),
    Column("last_order_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_in("kind", DEAL_KINDS), name="ck_observed_store_offers_kind"),
    CheckConstraint(_in("source", DEAL_SOURCES), name="ck_observed_store_offers_source"),
    # An automatic discount has no code; every other source is a code.
    CheckConstraint(
        "(source = 'checkout_automatic') = (code IS NULL)",
        name="ck_observed_store_offers_code_iff_coded_source",
    ),
    CheckConstraint(
        "code IS NOT NULL OR platform_rule_ref IS NOT NULL",
        name="ck_observed_store_offers_rule_identified",
    ),
    # Q13: a feed code names the program whose feed listed it, and only a feed code names one.
    CheckConstraint(
        "(source = 'authorized_feed') = (program_id IS NOT NULL)",
        name="ck_observed_store_offers_program_iff_feed",
    ),
    CheckConstraint(
        "scope_kind IS NULL OR scope_kind IN ('sitewide', 'collections', 'products')",
        name="ck_observed_store_offers_scope_kind",
    ),
    CheckConstraint("currency IS NULL OR length(currency) = 3", name="ck_observed_store_offers_currency"),
)
Index("uq_observed_store_offers_deal_key", observed_store_offers.c.deal_key, unique=True)
Index(
    "idx_observed_store_offers_seller_market",
    observed_store_offers.c.seller_id,
    observed_store_offers.c.market,
)


# One row per (deal, variant). A verification proves exactly that variant, at quantity 1, in the deal's
# market; a published scope never extends it (D3).
observed_store_offer_verifications = Table(
    "observed_store_offer_verifications",
    metadata,
    Column(
        "deal_id",
        String(64),
        ForeignKey("observed_store_offers.deal_id", ondelete="CASCADE"),
        nullable=False,
    ),
    # The platform variant id the reference cart and the cart permalink use.
    Column("variant_id", String(64), nullable=False),
    Column("canonical_variant_id", String(64), nullable=True),
    Column("state", String(24), nullable=False, server_default=text("'unverified'")),
    Column("verified_at", DateTime(timezone=True), nullable=True),
    # The earlier of the deal's ends_at and DEAL_VERIFY_TTL_HOURS after verified_at.
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("verified_via", String(40), nullable=True),
    # Which permission allowed the test: connected | q2_approval | program:<program_id> (D6, Q13).
    Column("authorized_by", String(160), nullable=True),
    Column("ref_quantity", Integer, nullable=False, server_default=text("1")),
    # The reference cart. Invalidation compares the variant's current observed price, compare-at price
    # and automatic discounts with these (D3); any change sets the row back to unverified.
    Column("ref_unit_price", Numeric(18, 4), nullable=True),
    Column("ref_compare_at_price", Numeric(18, 4), nullable=True),
    Column("ref_subtotal", Numeric(18, 4), nullable=True),
    Column("ref_discount_amount", Numeric(18, 4), nullable=True),
    Column("ref_currency", String(3), nullable=True),
    Column("ref_automatic_discounts", JSONB_TYPE, nullable=True),
    Column("failure_reason", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("deal_id", "variant_id"),
    CheckConstraint(_in("state", VERIFICATION_STATES), name="ck_observed_store_offer_verifications_state"),
    CheckConstraint(
        f"verified_via IS NULL OR {_in('verified_via', VERIFIED_VIA)}",
        name="ck_observed_store_offer_verifications_via",
    ),
    CheckConstraint(
        "authorized_by IS NULL OR authorized_by IN ('connected', 'q2_approval') "
        "OR authorized_by LIKE 'program:_%'",
        name="ck_observed_store_offer_verifications_authorized_by",
    ),
    CheckConstraint("ref_quantity = 1", name="ck_observed_store_offer_verifications_quantity_one"),
    # A verified row carries the whole reference cart, or it is not verified.
    CheckConstraint(
        "state <> 'checkout_verified' OR ("
        "verified_at IS NOT NULL AND expires_at IS NOT NULL AND expires_at > verified_at "
        "AND verified_via IS NOT NULL AND authorized_by IS NOT NULL "
        "AND ref_unit_price IS NOT NULL AND ref_subtotal IS NOT NULL AND ref_currency IS NOT NULL "
        "AND ref_discount_amount IS NOT NULL AND ref_discount_amount > 0)",
        name="ck_observed_store_offer_verifications_verified_is_complete",
    ),
)
Index(
    "idx_observed_store_offer_verifications_variant",
    observed_store_offer_verifications.c.variant_id,
    observed_store_offer_verifications.c.state,
)


# Q13: being an approved publisher in a program is permission to test THAT program's feed codes, where
# the program's terms or the merchant in writing allow automated checks. Recorded by a person after
# reading them. Current only while membership is active, the network has reported no terms change since
# confirmed_at, and reconfirm_by (at most 90 days after confirmed_at) has not passed.
deal_program_permissions = Table(
    "deal_program_permissions",
    metadata,
    Column("program_id", String(128), primary_key=True),
    Column("network", String(64), nullable=False),
    Column("membership_status", String(16), nullable=False),
    Column("permission_source", String(24), nullable=False),
    # Where the terms or the written agreement are kept.
    Column("permission_ref", Text, nullable=False),
    Column("permission_dated", DateTime(timezone=True), nullable=False),
    Column("confirmed_at", DateTime(timezone=True), nullable=False),
    Column("confirmed_by", String(128), nullable=False),
    Column("reconfirm_by", DateTime(timezone=True), nullable=False),
    Column("terms_changed_at", DateTime(timezone=True), nullable=True),
    Column("note", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "membership_status IN ('active', 'pending', 'suspended', 'ended')",
        name="ck_deal_program_permissions_membership",
    ),
    CheckConstraint(
        "permission_source IN ('program_terms', 'merchant_written')",
        name="ck_deal_program_permissions_source",
    ),
    CheckConstraint("reconfirm_by > confirmed_at", name="ck_deal_program_permissions_reconfirm_after_confirm"),
)

deal_program_permission_hosts = Table(
    "deal_program_permission_hosts",
    metadata,
    Column(
        "program_id",
        String(128),
        ForeignKey("deal_program_permissions.program_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("host", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("program_id", "host"),
)
Index("idx_deal_program_permission_hosts_host", deal_program_permission_hosts.c.host)


# Q14: one row per (deal, variant, market). `/r` chooses between the two links at click time; neither is
# ever handed out directly, and the coded link never enters a token (tokens are signed, not encrypted).
# A regenerated link replaces the old one in place, so issued tokens reach the current coded link.
deal_network_links = Table(
    "deal_network_links",
    metadata,
    Column("link_row_id", String(64), primary_key=True),
    Column(
        "deal_id",
        String(64),
        ForeignKey("observed_store_offers.deal_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("variant_id", String(64), nullable=False),
    Column("market", String(8), nullable=False),
    Column("program_id", String(128), nullable=False),
    Column("tracking_link", Text, nullable=False),
    Column("tracking_generated_at", DateTime(timezone=True), nullable=False),
    Column("tracking_expires_at", DateTime(timezone=True), nullable=True),
    Column("coded_link", Text, nullable=False),
    Column("coded_generated_at", DateTime(timezone=True), nullable=False),
    Column("coded_expires_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("coded_link <> tracking_link", name="ck_deal_network_links_distinct"),
)
Index(
    "uq_deal_network_links_deal_variant_market",
    deal_network_links.c.deal_id,
    deal_network_links.c.variant_id,
    deal_network_links.c.market,
    unique=True,
)


# D3: DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY, counted here because the verifier may run as several
# processes and several runs a day. A slot is reserved before a cart is built.
deal_verify_host_daily_carts = Table(
    "deal_verify_host_daily_carts",
    metadata,
    Column("host", String(256), nullable=False),
    Column("utc_day", Date, nullable=False),
    Column("carts", Integer, nullable=False, server_default=text("0")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("host", "utc_day"),
    CheckConstraint("carts >= 0", name="ck_deal_verify_host_daily_carts_nonnegative"),
)


# D7 stop rule: while a market has an uncleared row, serving, the deal decision and the `/r` re-check
# behave as if the emit, ranking and discount-param flags were off, including for links already issued.
# Only a person clears one.
deal_lane_stops = Table(
    "deal_lane_stops",
    metadata,
    Column("stop_id", String(64), primary_key=True),
    Column("market", String(8), nullable=False),
    Column("stop_trigger", String(16), nullable=False),
    Column("detail", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("cleared_at", DateTime(timezone=True), nullable=True),
    Column("cleared_by", String(128), nullable=True),
    Column("clear_note", Text, nullable=True),
    CheckConstraint(_in("stop_trigger", STOP_TRIGGERS), name="ck_deal_lane_stops_trigger"),
    CheckConstraint("cleared_at IS NULL OR cleared_by IS NOT NULL", name="ck_deal_lane_stops_cleared_by_person"),
)
Index(
    "uq_deal_lane_stops_active_market",
    deal_lane_stops.c.market,
    unique=True,
    postgresql_where=text("cleared_at IS NULL"),
    sqlite_where=text("cleared_at IS NULL"),
)


# D2: a seller that asked once is never tested, and its deals are never shown.
deal_seller_optouts = Table(
    "deal_seller_optouts",
    metadata,
    Column("seller_id", String(128), primary_key=True),
    Column("reason", Text, nullable=True),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("recorded_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def observed_store_offer_key(
    *,
    seller_id: str,
    market: Optional[str],
    code: Optional[str],
    platform_rule_ref: Optional[str],
    program_id: Optional[str],
) -> str:
    """The deal_key of a deal's identity. Shopify matches discount codes case-insensitively, so the
    code is compared upper-cased; every other part is compared as given (trimmed)."""
    parts = (
        (seller_id or "").strip(),
        (market or "").strip().upper(),
        (code or "").strip().upper(),
        (platform_rule_ref or "").strip(),
        (program_id or "").strip(),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
