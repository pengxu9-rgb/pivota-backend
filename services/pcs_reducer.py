from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from utils.logger import logger


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            # Allow both Z and offset forms.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def deterministic_fact_sort_key(fact: Dict[str, Any]) -> Tuple[datetime, datetime, str]:
    """
    Deterministic ordering rule (stable across runs):
    1) occurred_at (event time) ascending; missing -> epoch
    2) received_at (ingest time) ascending; missing -> epoch
    3) fact_id (uuid/text) ascending; missing -> empty string
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    occurred = _parse_ts(fact.get("occurred_at")) or epoch
    received = _parse_ts(fact.get("received_at")) or epoch
    fid = str(fact.get("fact_id") or "")
    return occurred, received, fid


def _safe_money(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _extract_shopify_totals(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Keep totals/currency minimal and best-effort.
    currency = payload.get("currency") or payload.get("presentment_currency")
    total = (
        payload.get("current_total_price")
        or payload.get("total_price")
        or payload.get("current_total_price_set", {}).get("shop_money", {}).get("amount")
    )
    subtotal = payload.get("subtotal_price") or payload.get(
        "subtotal_price_set", {}
    ).get("shop_money", {}).get("amount")
    tax = payload.get("total_tax") or payload.get("total_tax_set", {}).get("shop_money", {}).get("amount")
    discount_total = payload.get("total_discounts") or payload.get(
        "total_discounts_set", {}
    ).get("shop_money", {}).get("amount")
    out: Dict[str, Any] = {}
    if currency:
        out["currency"] = str(currency)
    if total is not None:
        out["total"] = _safe_money(total)
    if subtotal is not None:
        out["subtotal"] = _safe_money(subtotal)
    if tax is not None:
        out["tax"] = _safe_money(tax)
    if discount_total is not None:
        out["discount_total"] = _safe_money(discount_total)
    return out


def reduce_order_facts(facts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Reduce an order's facts into a minimal current-state JSON.

    Idempotency:
    - Dedupes by dedupe_key (if present).
    - Sorts by deterministic_fact_sort_key.

    Convergence under out-of-order facts:
    - Any ordering of the same fact set converges to the same output due to the deterministic sort.
    """
    seen: set[str] = set()
    ordered: List[Dict[str, Any]] = []
    for f in facts or []:
        dk = str(f.get("dedupe_key") or "")
        if dk and dk in seen:
            continue
        if dk:
            seen.add(dk)
        ordered.append(dict(f))

    ordered.sort(key=deterministic_fact_sort_key)

    state: Dict[str, Any] = {
        "order_status": None,
        "financial_status": None,
        "fulfillment_status": None,
        "last_update_at": None,
        "currency": None,
        "totals": {},
        "quote": None,
        "dispute": None,
    }

    last_fact_occurred_at: Optional[datetime] = None
    last_dedupe_key: Optional[str] = None

    for fact in ordered:
        fact_type = str(fact.get("fact_type") or "")
        payload = fact.get("payload_json") or fact.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {"raw": payload}

        occurred_at = _parse_ts(fact.get("occurred_at")) or _parse_ts(fact.get("received_at")) or _utc_now()
        last_fact_occurred_at = occurred_at
        last_dedupe_key = str(fact.get("dedupe_key") or last_dedupe_key or "")

        # Shopify order payloads are the primary source of truth.
        if fact_type.startswith("shopify.orders."):
            # Minimal status signals.
            cancelled_at = payload.get("cancelled_at")
            closed_at = payload.get("closed_at")
            raw_order_status = payload.get("status") or payload.get("order_status") or "open"
            if cancelled_at:
                state["order_status"] = "cancelled"
            elif closed_at:
                state["order_status"] = "closed"
            else:
                state["order_status"] = str(raw_order_status)

            if payload.get("financial_status") is not None:
                state["financial_status"] = str(payload.get("financial_status"))
            if payload.get("fulfillment_status") is not None:
                state["fulfillment_status"] = str(payload.get("fulfillment_status"))

            totals = _extract_shopify_totals(payload)
            if totals.get("currency") and not state.get("currency"):
                state["currency"] = totals.get("currency")
            if totals:
                state["totals"] = {**(state.get("totals") or {}), **totals}

        elif fact_type.startswith("shopify.fulfillments."):
            # Fulfillment webhooks can arrive out-of-order relative to orders/updated.
            status = payload.get("status") or payload.get("shipment_status")
            if status:
                # Best-effort normalization.
                s = str(status).lower()
                if s in ("success", "delivered", "fulfilled"):
                    state["fulfillment_status"] = "fulfilled"
                elif s in ("partial", "in_transit", "out_for_delivery"):
                    state["fulfillment_status"] = "partial"

        elif fact_type.startswith("shopify.refunds."):
            # Minimal refund signal; Shopify may also emit orders/updated with financial_status changed.
            if state.get("financial_status") not in ("refunded", "partially_refunded"):
                state["financial_status"] = "refunded"

        elif fact_type.startswith("shopify.disputes."):
            dispute_status = payload.get("status") or payload.get("state") or fact.get("topic")
            if dispute_status:
                state["dispute"] = {"status": str(dispute_status)}

        elif fact_type.startswith("internal."):
            # Internal facts (no PII): quote refs, payment refs, evidence pack refs.
            if fact_type == "internal.order_created":
                quote_id = payload.get("quote_id")
                quote_hash = payload.get("quote_hash_sha256")
                if quote_id or quote_hash:
                    state["quote"] = {"quote_id": quote_id, "quote_hash_sha256": quote_hash}
                if payload.get("currency") and not state.get("currency"):
                    state["currency"] = payload.get("currency")
                if payload.get("total") is not None:
                    state["totals"] = {**(state.get("totals") or {}), "total": _safe_money(payload.get("total"))}
            elif fact_type == "internal.payment_updated":
                status = payload.get("status")
                if status:
                    s = str(status).lower()
                    if s in ("succeeded", "paid"):
                        state["financial_status"] = "paid"
                    elif s in ("failed", "canceled", "cancelled"):
                        state["financial_status"] = "failed"
            elif fact_type == "internal.refund_processed":
                state["financial_status"] = "refunded"
            elif fact_type == "internal.evidence_pack_frozen":
                # Informational; do not include manifest itself.
                pass

        state["last_update_at"] = occurred_at.isoformat()

    return {
        "current_state_json": state,
        "last_fact_occurred_at": last_fact_occurred_at,
        "last_dedupe_key": last_dedupe_key,
    }


class ReducerStore(Protocol):
    async def get_checkpoint(self, *, merchant_id: str, stream_id: str, reducer_name: str) -> Optional[Dict[str, Any]]: ...
    async def upsert_checkpoint(self, *, merchant_id: str, stream_id: str, reducer_name: str, checkpoint: Dict[str, Any]) -> None: ...
    async def list_new_facts(
        self,
        *,
        merchant_id: str,
        stream_id: str,
        since_received_at: Optional[datetime],
        since_row_id: Optional[int],
        limit: int,
    ) -> List[Dict[str, Any]]: ...
    async def list_all_facts_for_order(self, *, merchant_id: str, stream_id: str, order_id: str) -> List[Dict[str, Any]]: ...
    async def upsert_orders_current(
        self,
        *,
        merchant_id: str,
        order_id: str,
        current_state_json: Dict[str, Any],
        last_fact_occurred_at: Optional[datetime],
        last_dedupe_key: Optional[str],
    ) -> None: ...


@dataclass(frozen=True)
class ReducerResult:
    merchant_id: str
    stream_id: str
    reducer_name: str
    facts_scanned: int
    facts_applied: int
    orders_updated: int
    duration_ms: int
    checkpoint: Dict[str, Any]


class PostgresReducerStore:
    def __init__(self, db=None):
        if db is None:
            from db.database import database as db  # local import

        self.db = db

    async def get_checkpoint(self, *, merchant_id: str, stream_id: str, reducer_name: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            """
            SELECT checkpoint_json
            FROM pcs_reducer_checkpoints
            WHERE merchant_id = :merchant_id AND stream_id = :stream_id AND reducer_name = :reducer_name
            """,
            {"merchant_id": merchant_id, "stream_id": stream_id, "reducer_name": reducer_name},
        )
        if not row:
            return None
        cj = row.get("checkpoint_json")
        if isinstance(cj, dict):
            return cj
        if isinstance(cj, str):
            try:
                return json.loads(cj)
            except Exception:
                return {"raw": cj}
        return {"raw": str(cj)}

    async def upsert_checkpoint(self, *, merchant_id: str, stream_id: str, reducer_name: str, checkpoint: Dict[str, Any]) -> None:
        await self.db.execute(
            """
            INSERT INTO pcs_reducer_checkpoints (merchant_id, stream_id, reducer_name, checkpoint_json, updated_at)
            VALUES (:merchant_id, :stream_id, :reducer_name, CAST(:checkpoint_json AS jsonb), NOW())
            ON CONFLICT (merchant_id, stream_id, reducer_name)
            DO UPDATE SET checkpoint_json = EXCLUDED.checkpoint_json, updated_at = NOW()
            """,
            {
                "merchant_id": merchant_id,
                "stream_id": stream_id,
                "reducer_name": reducer_name,
                "checkpoint_json": json.dumps(checkpoint, ensure_ascii=False),
            },
        )

    async def list_new_facts(
        self,
        *,
        merchant_id: str,
        stream_id: str,
        since_received_at: Optional[datetime],
        since_row_id: Optional[int],
        limit: int,
    ) -> List[Dict[str, Any]]:
        values: Dict[str, Any] = {
            "merchant_id": merchant_id,
            "stream_id": stream_id,
            "limit": max(1, min(int(limit), 50000)),
        }
        where = "merchant_id = :merchant_id AND stream_id = :stream_id"
        if since_received_at is not None:
            values["since_received_at"] = since_received_at
            if since_row_id is None:
                where += " AND received_at >= :since_received_at"
            else:
                values["since_row_id"] = int(since_row_id)
                where += " AND (received_at > :since_received_at OR (received_at = :since_received_at AND id > :since_row_id))"

        rows = await self.db.fetch_all(
            f"""
            SELECT id, merchant_id, stream_id, order_id, fact_id, fact_type, occurred_at, received_at,
                   source, topic, source_event_id, dedupe_key, payload_json, payload_sha256, chain_hash
            FROM pcs_order_facts
            WHERE {where}
            ORDER BY received_at ASC, id ASC
            LIMIT :limit
            """,
            values,
        )
        return [dict(r) for r in (rows or [])]

    async def list_all_facts_for_order(self, *, merchant_id: str, stream_id: str, order_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT id, merchant_id, stream_id, order_id, fact_id, fact_type, occurred_at, received_at,
                   source, topic, source_event_id, dedupe_key, payload_json, payload_sha256, chain_hash
            FROM pcs_order_facts
            WHERE merchant_id = :merchant_id AND stream_id = :stream_id AND order_id = :order_id
            ORDER BY occurred_at ASC NULLS FIRST, received_at ASC, fact_id ASC
            """,
            {"merchant_id": merchant_id, "stream_id": stream_id, "order_id": order_id},
        )
        return [dict(r) for r in (rows or [])]

    async def upsert_orders_current(
        self,
        *,
        merchant_id: str,
        order_id: str,
        current_state_json: Dict[str, Any],
        last_fact_occurred_at: Optional[datetime],
        last_dedupe_key: Optional[str],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO pcs_orders_current
              (merchant_id, order_id, current_state_json, last_fact_occurred_at, last_reduced_at, last_dedupe_key)
            VALUES
              (:merchant_id, :order_id, CAST(:current_state_json AS jsonb), :last_fact_occurred_at, NOW(), :last_dedupe_key)
            ON CONFLICT (merchant_id, order_id)
            DO UPDATE SET
              current_state_json = EXCLUDED.current_state_json,
              last_fact_occurred_at = EXCLUDED.last_fact_occurred_at,
              last_reduced_at = NOW(),
              last_dedupe_key = EXCLUDED.last_dedupe_key
            """,
            {
                "merchant_id": merchant_id,
                "order_id": order_id,
                "current_state_json": json.dumps(current_state_json, ensure_ascii=False),
                "last_fact_occurred_at": last_fact_occurred_at,
                "last_dedupe_key": last_dedupe_key,
            },
        )


async def reduce_merchant(
    *,
    merchant_id: str,
    stream_id: str = "orders",
    since: Optional[datetime] = None,
    limit: int = 5000,
    reducer_name: str = "pcs_orders_current_v0_2",
    store: Optional[ReducerStore] = None,
) -> ReducerResult:
    """
    Reduce a merchant's facts into deterministic per-order current state.

    Incremental replay strategy (out-of-order safe):
    - Scan new facts by received_at >= max(checkpoint.last_received_at, since).
    - For each impacted order_id, recompute the order's current state by replaying *all* facts for that order
      sorted by deterministic_fact_sort_key. This handles late-arriving facts with earlier occurred_at.
    """
    started = time.time()
    if store is None:
        store = PostgresReducerStore()

    # Observability (best-effort, no PII).
    try:
        from mvp.events import emit_best_effort
        from mvp.constants import SURFACE_BACKEND

        emit_best_effort(
            event_type="reducer_run_started",
            payload={"merchant_id": merchant_id, "stream_id": stream_id, "reducer_name": reducer_name},
            merchant_id=merchant_id,
            geo=None,
            surface=SURFACE_BACKEND,
            adapter="pcs_reducer",
            risk_tier="unknown",
            idempotency_key=f"{merchant_id}:{stream_id}:{reducer_name}:{int(started)}",
        )
    except Exception:
        pass

    try:
        checkpoint = await store.get_checkpoint(
            merchant_id=merchant_id, stream_id=stream_id, reducer_name=reducer_name
        ) or {}
        last_received_at = _parse_ts(checkpoint.get("last_received_at"))
        last_row_id_raw = checkpoint.get("last_row_id")
        last_row_id = int(last_row_id_raw) if isinstance(last_row_id_raw, (int, float, str)) and str(last_row_id_raw).isdigit() else None

        scan_since = since if since is not None else last_received_at
        if since is not None and last_received_at is not None:
            scan_since = min(since, last_received_at)
        scan_since_row_id: Optional[int] = last_row_id if (last_received_at is not None and scan_since == last_received_at) else None

        new_facts = await store.list_new_facts(
            merchant_id=merchant_id,
            stream_id=stream_id,
            since_received_at=scan_since,
            since_row_id=scan_since_row_id,
            limit=limit,
        )

        impacted_orders: set[str] = set()
        max_received_at: Optional[datetime] = last_received_at
        max_row_id: Optional[int] = last_row_id
        for f in new_facts:
            oid = f.get("order_id")
            if oid:
                impacted_orders.add(str(oid))
            rcv = _parse_ts(f.get("received_at"))
            if rcv and (max_received_at is None or rcv > max_received_at):
                max_received_at = rcv
            rid = f.get("id")
            if isinstance(rid, int) and (max_row_id is None or rid > max_row_id):
                max_row_id = rid

        orders_updated = 0
        facts_applied = 0
        for order_id in sorted(impacted_orders):
            order_facts = await store.list_all_facts_for_order(
                merchant_id=merchant_id, stream_id=stream_id, order_id=order_id
            )
            reduced = reduce_order_facts(order_facts)
            await store.upsert_orders_current(
                merchant_id=merchant_id,
                order_id=order_id,
                current_state_json=reduced["current_state_json"],
                last_fact_occurred_at=reduced["last_fact_occurred_at"],
                last_dedupe_key=reduced["last_dedupe_key"],
            )
            orders_updated += 1
            facts_applied += len(order_facts)

        next_checkpoint = {
            "last_received_at": (max_received_at.isoformat() if max_received_at else None),
            "last_row_id": max_row_id,
            "orders_updated": orders_updated,
            "facts_scanned": len(new_facts),
        }
        await store.upsert_checkpoint(
            merchant_id=merchant_id, stream_id=stream_id, reducer_name=reducer_name, checkpoint=next_checkpoint
        )

        duration_ms = int((time.time() - started) * 1000)

        try:
            from mvp.events import emit_best_effort
            from mvp.constants import SURFACE_BACKEND

            emit_best_effort(
                event_type="reducer_run_completed",
                payload={
                    "merchant_id": merchant_id,
                    "stream_id": stream_id,
                    "reducer_name": reducer_name,
                    "facts_scanned": len(new_facts),
                    "facts_applied": facts_applied,
                    "orders_updated": orders_updated,
                    "duration_ms": duration_ms,
                },
                merchant_id=merchant_id,
                geo=None,
                surface=SURFACE_BACKEND,
                adapter="pcs_reducer",
                risk_tier="unknown",
                idempotency_key=f"{merchant_id}:{stream_id}:{reducer_name}:{int(started)}",
            )
        except Exception:
            pass

        return ReducerResult(
            merchant_id=merchant_id,
            stream_id=stream_id,
            reducer_name=reducer_name,
            facts_scanned=len(new_facts),
            facts_applied=facts_applied,
            orders_updated=orders_updated,
            duration_ms=duration_ms,
            checkpoint=next_checkpoint,
        )
    except Exception as e:
        duration_ms = int((time.time() - started) * 1000)
        logger.warning({"merchant_id": merchant_id, "error": str(e)}, "PCS reducer run failed")
        try:
            from mvp.events import emit_best_effort
            from mvp.constants import SURFACE_BACKEND

            emit_best_effort(
                event_type="reducer_run_failed",
                payload={
                    "merchant_id": merchant_id,
                    "stream_id": stream_id,
                    "reducer_name": reducer_name,
                    "duration_ms": duration_ms,
                    "error_code": type(e).__name__,
                },
                merchant_id=merchant_id,
                geo=None,
                surface=SURFACE_BACKEND,
                adapter="pcs_reducer",
                risk_tier="unknown",
                idempotency_key=f"{merchant_id}:{stream_id}:{reducer_name}:{int(started)}",
            )
        except Exception:
            pass
        raise
