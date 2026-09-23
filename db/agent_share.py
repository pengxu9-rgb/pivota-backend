"""Agent revenue-share tables (ADR-025 D5).

An agent's share is a percentage of PIVOTA'S COMMISSION on an attributed order, net of refunds,
never of the order value: when Pivota earns nothing on an order, the agent accrues nothing
(decision 2026-09-23). Rates live here, per agent, effective-dated, and the table starts empty,
so nothing accrues until a rate is set.

The legacy Phase 5 tables (`agent_revenue_policies`, `agent_revenue_logs`) are deliberately
NOT reused. Their only writer (core.agent_routing_controller.apply_revenue_split) has no
callers, and their 14 production rows were set for a PSP-routing model, so reusing them would
silently start accruing against rates nobody chose for this.

Created by `metadata.create_all` at startup (main.py imports this module), and by
db/migrations/235_agent_share_accrual.sql.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.sql import func

from db.database import metadata

#: One row per (agent, window). `effective_to` NULL means open-ended. Windows for one agent never
#: overlap; services.agent_share_accrual.set_agent_share_rate enforces that under a lock.
agent_share_rates = Table(
    "agent_share_rates",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("agent_id", String(64), nullable=False),
    # Share of Pivota's commission, in basis points: 2500 = 25% of what Pivota earns.
    Column("share_bp", Integer, nullable=False),
    Column("effective_from", DateTime(timezone=True), nullable=False),
    Column("effective_to", DateTime(timezone=True), nullable=True),
    Column("created_by", String(128), nullable=False),
    Column("note", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("share_bp BETWEEN 0 AND 10000", name="ck_agent_share_rates_share_bp"),
    CheckConstraint(
        "effective_to IS NULL OR effective_to > effective_from", name="ck_agent_share_rates_window"
    ),
)
Index("idx_agent_share_rates_agent_from", agent_share_rates.c.agent_id, agent_share_rates.c.effective_from)

#: Append-only. An edge's accrued share is SUM(amount_minor) over its rows; a recomputation that
#: finds a different target writes the signed difference, so history is never rewritten and a
#: refund shows up as its own negative row.
agent_share_ledger = Table(
    "agent_share_ledger",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("edge_id", String(64), nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("merchant_id", String(100), nullable=False),
    Column("currency", String(8), nullable=False),
    # accrual = first non-zero entry for the edge; adjustment = every later correction.
    Column("entry_kind", String(16), nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    # The inputs that produced this entry's target, so any row can be re-derived by hand.
    Column("net_gmv_minor", BigInteger, nullable=False),
    Column("take_rate_bp", Integer, nullable=False),
    Column("commission_minor", BigInteger, nullable=False),
    Column("share_bp", Integer, nullable=False),
    Column("rate_id", BigInteger, nullable=True),
    Column("target_minor", BigInteger, nullable=False),
    Column("commission_source", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("entry_kind IN ('accrual', 'adjustment')", name="ck_agent_share_ledger_kind"),
    CheckConstraint("amount_minor <> 0", name="ck_agent_share_ledger_nonzero"),
    CheckConstraint("target_minor >= 0", name="ck_agent_share_ledger_target_nonneg"),
)
Index("idx_agent_share_ledger_edge", agent_share_ledger.c.edge_id)
Index("idx_agent_share_ledger_agent_created", agent_share_ledger.c.agent_id, agent_share_ledger.c.created_at)
