"""Outcome aggregation — the durable-moat pipeline.

Rolls the decision -> order -> paid/refund loop up into per-merchant and
per-product OUTCOME metrics (rail_transacted, refund_rate, GMV) and stores them in
`aggregated_outcomes`. This is the proprietary, least-copyable signal class: what
actually happened after an agent routed a buyer through Pivota.

HONESTY DISCIPLINE (load-bearing, pre-launch data is tiny + test-concentrated):
- a rate (refund_rate) is ONLY surfaced when transacted_count >= MIN_SAMPLE_SIZE;
  below that the rate is NULL and min_sample_met is False. We never imply a return
  rate from a handful of orders.
- counts (transacted/paid/refunded) and GMV are always real sums, never estimates.

Sources:
- merchant outcomes  ← `orders` grouped by merchant_id.
- product outcomes   ← `commerce_attribution_edges` (canonical_product_id, gmv) JOIN
                        `orders` for payment status. (checkout_decisions.content_key
                        is not yet populated, so product attribution rides the
                        attribution edge, which already carries canonical_product_id.)

The aggregator is idempotent (UPSERT) and safe to re-run; a scheduled job refreshes
it. Reads (get_outcomes) gate on min_sample_met for any rate-bearing consumer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

# Orders that completed payment (the denominator for outcomes).
TRANSACTED_STATUSES = ("paid", "refunded", "partially_refunded")
REFUNDED_STATUSES = ("refunded", "partially_refunded")

# A rate is suppressed until at least this many transacted orders exist for the subject.
MIN_SAMPLE_SIZE = int(os.getenv("OUTCOMES_MIN_SAMPLE_SIZE", "20"))
TRAILING_WINDOW_DAYS = int(os.getenv("OUTCOMES_TRAILING_WINDOW_DAYS", "90"))

_DDL_READY = False
_ddl_lock_obj: Optional[asyncio.Lock] = None


def _ddl_lock() -> asyncio.Lock:
    global _ddl_lock_obj
    if _ddl_lock_obj is None:
        _ddl_lock_obj = asyncio.Lock()
    return _ddl_lock_obj


async def ensure_aggregated_outcomes_table() -> None:
    """Create the table at runtime (prod skips the startup migration runner)."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _ddl_lock():
        if _DDL_READY:
            return
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS aggregated_outcomes (
              subject_type     VARCHAR(16)  NOT NULL,
              subject_key      VARCHAR(128) NOT NULL,
              window_key       VARCHAR(16)  NOT NULL DEFAULT 'all_time',
              transacted_count INTEGER      NOT NULL DEFAULT 0,
              paid_count       INTEGER      NOT NULL DEFAULT 0,
              refunded_count   INTEGER      NOT NULL DEFAULT 0,
              refund_rate      NUMERIC(5,4),
              gmv_cents        BIGINT       NOT NULL DEFAULT 0,
              aov_cents        BIGINT,
              currency         VARCHAR(8),
              sample_size      INTEGER      NOT NULL DEFAULT 0,
              min_sample_met   BOOLEAN      NOT NULL DEFAULT FALSE,
              computed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
              CONSTRAINT aggregated_outcomes_pkey PRIMARY KEY (subject_type, subject_key, window_key)
            )
            """
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_aggregated_outcomes_subject "
            "ON aggregated_outcomes (subject_type, subject_key)"
        )
        _DDL_READY = True


# Window clause: all_time has no lower bound; trailing_90d bounds on created_at.
def _window_clause(window_key: str, alias: str) -> str:
    if window_key == "trailing_90d":
        return f"AND {alias}.created_at >= now() - interval '{TRAILING_WINDOW_DAYS} days'"
    return ""


def _merchant_sql(window_key: str) -> str:
    return f"""
        SELECT
            merchant_id AS subject_key,
            count(*) FILTER (WHERE payment_status = ANY(:transacted))                       AS transacted_count,
            count(*) FILTER (WHERE payment_status = 'paid')                                  AS paid_count,
            count(*) FILTER (WHERE payment_status = ANY(:refunded))                          AS refunded_count,
            COALESCE(SUM((total)::numeric) FILTER (WHERE payment_status = ANY(:transacted)), 0) AS gmv_units,
            MAX(currency)                                                                    AS currency
        FROM orders o
        WHERE merchant_id IS NOT NULL
          {_window_clause(window_key, 'o')}
        GROUP BY merchant_id
        HAVING count(*) FILTER (WHERE payment_status = ANY(:transacted)) > 0
    """


def _product_sql(window_key: str) -> str:
    return f"""
        SELECT
            cae.canonical_product_id AS subject_key,
            count(*) FILTER (WHERE o.payment_status = ANY(:transacted))                       AS transacted_count,
            count(*) FILTER (WHERE o.payment_status = 'paid')                                 AS paid_count,
            count(*) FILTER (WHERE o.payment_status = ANY(:refunded))                         AS refunded_count,
            COALESCE(SUM(cae.gross_attributed_gmv_cents) FILTER (WHERE o.payment_status = ANY(:transacted)), 0) AS gmv_cents,
            MAX(o.currency)                                                                   AS currency
        FROM commerce_attribution_edges cae
        JOIN orders o ON o.order_id = cae.order_id
        WHERE cae.canonical_product_id IS NOT NULL
          {_window_clause(window_key, 'o')}
        GROUP BY cae.canonical_product_id
        HAVING count(*) FILTER (WHERE o.payment_status = ANY(:transacted)) > 0
    """


UPSERT_SQL = """
    INSERT INTO aggregated_outcomes
      (subject_type, subject_key, window_key, transacted_count, paid_count, refunded_count,
       refund_rate, gmv_cents, aov_cents, currency, sample_size, min_sample_met, computed_at)
    VALUES
      (:subject_type, :subject_key, :window_key, :transacted_count, :paid_count, :refunded_count,
       :refund_rate, :gmv_cents, :aov_cents, :currency, :sample_size, :min_sample_met, now())
    ON CONFLICT (subject_type, subject_key, window_key) DO UPDATE SET
      transacted_count = EXCLUDED.transacted_count,
      paid_count       = EXCLUDED.paid_count,
      refunded_count   = EXCLUDED.refunded_count,
      refund_rate      = EXCLUDED.refund_rate,
      gmv_cents        = EXCLUDED.gmv_cents,
      aov_cents        = EXCLUDED.aov_cents,
      currency         = EXCLUDED.currency,
      sample_size      = EXCLUDED.sample_size,
      min_sample_met   = EXCLUDED.min_sample_met,
      computed_at      = now()
"""


def _build_row(subject_type: str, window_key: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    transacted = int(raw.get("transacted_count") or 0)
    paid = int(raw.get("paid_count") or 0)
    refunded = int(raw.get("refunded_count") or 0)
    # gmv: merchant rows return gmv_units (currency units) → cents; product rows return cents already.
    if "gmv_cents" in raw and raw.get("gmv_cents") is not None:
        gmv_cents = int(raw.get("gmv_cents") or 0)
    else:
        try:
            gmv_cents = int(round(float(raw.get("gmv_units") or 0) * 100))
        except Exception:
            gmv_cents = 0
    min_sample_met = transacted >= MIN_SAMPLE_SIZE
    # Rate ONLY when the sample is large enough — never imply a return rate from a few orders.
    refund_rate = round(refunded / transacted, 4) if (min_sample_met and transacted > 0) else None
    aov_cents = int(round(gmv_cents / transacted)) if transacted > 0 else None
    return {
        "subject_type": subject_type,
        "subject_key": str(raw.get("subject_key")),
        "window_key": window_key,
        "transacted_count": transacted,
        "paid_count": paid,
        "refunded_count": refunded,
        "refund_rate": refund_rate,
        "gmv_cents": gmv_cents,
        "aov_cents": aov_cents,
        "currency": raw.get("currency"),
        "sample_size": transacted,
        "min_sample_met": min_sample_met,
    }


async def _aggregate_one(subject_type: str, window_key: str, sql: str) -> int:
    rows = await database.fetch_all(
        sql, {"transacted": list(TRANSACTED_STATUSES), "refunded": list(REFUNDED_STATUSES)}
    )
    written = 0
    for r in rows or []:
        params = _build_row(subject_type, window_key, dict(r))
        if not params["subject_key"] or params["subject_key"] == "None":
            continue
        await database.execute(UPSERT_SQL, params)
        written += 1
    return written


async def refresh_all_outcomes() -> Dict[str, int]:
    """Recompute every (subject_type × window) and upsert. Idempotent; safe to re-run."""
    await ensure_aggregated_outcomes_table()
    counts: Dict[str, int] = {}
    for window_key in ("all_time", "trailing_90d"):
        try:
            counts[f"merchant:{window_key}"] = await _aggregate_one(
                "merchant", window_key, _merchant_sql(window_key)
            )
        except Exception as exc:  # noqa: BLE001 — one grain's failure must not sink the others
            logger.warning("outcome aggregation merchant/%s failed: %s", window_key, exc)
            counts[f"merchant:{window_key}"] = -1
        try:
            counts[f"product:{window_key}"] = await _aggregate_one(
                "product", window_key, _product_sql(window_key)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome aggregation product/%s failed: %s", window_key, exc)
            counts[f"product:{window_key}"] = -1
    logger.info("outcome aggregation refreshed: %s", counts)
    return counts


async def get_outcomes(
    subject_type: str, subject_key: str, *, window_key: str = "all_time"
) -> Optional[Dict[str, Any]]:
    """Read one subject's outcomes. refund_rate is already None unless min_sample_met."""
    await ensure_aggregated_outcomes_table()
    row = await database.fetch_one(
        "SELECT * FROM aggregated_outcomes WHERE subject_type = :st AND subject_key = :sk AND window_key = :wk",
        {"st": subject_type, "sk": subject_key, "wk": window_key},
    )
    return dict(row) if row else None


async def get_merchant_outcomes(merchant_id: str, *, window_key: str = "all_time") -> Optional[Dict[str, Any]]:
    return await get_outcomes("merchant", merchant_id, window_key=window_key)


# Return-rate bands. Only assigned when min_sample_met — never inferred from a
# handful of orders. Thresholds are deliberately coarse (the honest resolution of
# a pre-launch sample): the raw refund_rate rides alongside for agents that want it.
def _return_rate_band(refund_rate: Optional[float]) -> Optional[str]:
    if refund_rate is None:
        return None
    if refund_rate <= 0.05:
        return "low"
    if refund_rate <= 0.15:
        return "moderate"
    return "elevated"


def seller_trust_from_outcome(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pure: turn one aggregated_outcomes merchant row into an honest seller-trust
    envelope, or None when there's nothing real to say.

    The moat signal is *what actually happened after an agent routed a buyer here*.
    Discipline (mirrors the aggregator): counts are always real sums; a
    return-rate + band appear ONLY when min_sample_met (transacted_count >=
    MIN_SAMPLE_SIZE). Below that the envelope still carries the honest transacted
    volume so an agent can weigh it — it just makes no rate claim. Returns None for
    a merchant with zero transacted orders (no signal, not a fabricated zero)."""
    if not row:
        return None
    transacted = int(row.get("transacted_count") or 0)
    if transacted <= 0:
        return None
    min_met = bool(row.get("min_sample_met"))
    raw_rate = row.get("refund_rate")
    # refund_rate is already NULL below sample in the store; re-gate defensively.
    refund_rate = float(raw_rate) if (min_met and raw_rate is not None) else None
    computed_at = row.get("computed_at")
    return {
        "merchant_id": str(row.get("subject_key") or ""),
        "window": str(row.get("window_key") or "all_time"),
        "transacted_count": transacted,
        "paid_count": int(row.get("paid_count") or 0),
        "refunded_count": int(row.get("refunded_count") or 0),
        "gmv_cents": int(row.get("gmv_cents") or 0),
        "currency": row.get("currency"),
        # Sample-gated: rate + band only when the volume backs it.
        "sample_backed": min_met,
        "return_rate": refund_rate,
        "return_rate_band": _return_rate_band(refund_rate),
        "computed_at": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
    }


async def get_seller_trust(merchant_id: str, *, window_key: str = "all_time") -> Optional[Dict[str, Any]]:
    """The outcome-derived seller-trust signal for one merchant (W8 part B).

    Reads the existing aggregated_outcomes store — no new table — and shapes the
    honest envelope. None when the merchant has no transacted outcomes yet (the
    correct empty-data behavior: fail-closed, never a fabricated trust score)."""
    return seller_trust_from_outcome(await get_merchant_outcomes(merchant_id, window_key=window_key))


async def seller_trust_bulk(
    merchant_ids: List[str], *, window_key: str = "all_time"
) -> Dict[str, Dict[str, Any]]:
    """{merchant_id -> seller_trust envelope} for the merchants that have real
    transacted outcomes. One query (no N+1); merchants with no outcome row simply
    don't appear (the caller treats absence as "no signal yet")."""
    ids = sorted({str(m) for m in (merchant_ids or []) if m})
    if not ids:
        return {}
    await ensure_aggregated_outcomes_table()
    rows = await database.fetch_all(
        "SELECT * FROM aggregated_outcomes "
        "WHERE subject_type = 'merchant' AND window_key = :wk AND subject_key = ANY(:ids)",
        {"wk": window_key, "ids": ids},
    )
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        env = seller_trust_from_outcome(dict(row))
        if env:
            out[str(row["subject_key"])] = env
    return out
