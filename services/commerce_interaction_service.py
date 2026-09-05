from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, select

from db.commerce_interactions import commerce_interaction_events, commerce_interactions
from db.database import IS_POSTGRES, database
from services.commerce_ledger_provenance import resolve_ledger_authority
from services.traffic_taxonomy_service import (
    TRAFFIC_TAXONOMY_FIELDS,
    attach_traffic_taxonomy,
    build_traffic_taxonomy,
)


logger = logging.getLogger("commerce_interaction_service")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _make_json_safe(payload: Any) -> Any:
    return json.loads(json.dumps(payload, default=_json_default, ensure_ascii=False))


def _stable_id(prefix: str, *parts: Any) -> str:
    normalized = "|".join(str(part or "").strip().lower() for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _coerce_refs(payload: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Optional[str]]:
    raw = dict(payload or {})
    raw.update(kwargs)
    refs = {
        "interaction_id": _normalize_text(raw.get("interaction_id")),
        "merchant_id": _normalize_text(raw.get("merchant_id")),
        "platform": _normalize_text(raw.get("platform")),
        "store_id": _normalize_text(raw.get("store_id")),
        "surface": _normalize_text(raw.get("surface")),
        "prompt_id": _normalize_text(raw.get("prompt_id")),
        "result_id": _normalize_text(raw.get("result_id")),
        "click_id": _normalize_text(raw.get("click_id")),
        "cart_id": _normalize_text(raw.get("cart_id")),
        "quote_id": _normalize_text(raw.get("quote_id")),
        "checkout_id": _normalize_text(raw.get("checkout_id")),
        "payment_id": _normalize_text(raw.get("payment_id")),
        "order_id": _normalize_text(raw.get("order_id")),
        "refund_id": _normalize_text(raw.get("refund_id")),
        "return_id": _normalize_text(raw.get("return_id")),
        "canonical_product_id": _normalize_text(raw.get("canonical_product_id")),
        "canonical_variant_id": _normalize_text(raw.get("canonical_variant_id")),
        "trace_id": _normalize_text(raw.get("trace_id")),
        "brief_id": _normalize_text(raw.get("brief_id")),
        "session_id": _normalize_text(raw.get("session_id")),
        "visitor_id": _normalize_text(raw.get("visitor_id")),
        "buyer_id": _normalize_text(raw.get("buyer_id")),
    }
    return refs


def _fallback_interaction_id(refs: Dict[str, Optional[str]]) -> str:
    for key in (
        "click_id",
        "cart_id",
        "order_id",
        "checkout_id",
        "payment_id",
        "quote_id",
        "refund_id",
        "return_id",
        "prompt_id",
        "result_id",
        "trace_id",
        "session_id",
        "canonical_variant_id",
        "canonical_product_id",
    ):
        value = refs.get(key)
        if value:
            scope = [refs.get("merchant_id")]
            if refs.get("store_id"):
                scope.append(refs["store_id"])
            return _stable_id("int", *scope, key, value)
    return f"int_{uuid.uuid4().hex[:24]}"


_INTERACTION_LOOKUP_KEYS = (
    "interaction_id",
    "click_id",
    "order_id",
    "checkout_id",
    "payment_id",
    "cart_id",
    "quote_id",
    "refund_id",
    "return_id",
    "session_id",
)

_STORE_SCOPED_LOOKUP_KEYS = {
    "interaction_id",
    "click_id",
    "session_id",
    "cart_id",
    "payment_id",
    "order_id",
    "checkout_id",
    "quote_id",
    "refund_id",
    "return_id",
}

_WEAK_INTERACTION_LOOKUP_KEYS = {"session_id", "cart_id", "payment_id"}
_INTERACTION_SELECTION_KEYS = tuple(
    key for key in _INTERACTION_LOOKUP_KEYS if key not in _WEAK_INTERACTION_LOOKUP_KEYS
) + tuple(key for key in _INTERACTION_LOOKUP_KEYS if key in _WEAK_INTERACTION_LOOKUP_KEYS)


def _interaction_lookup_conditions(
    refs: Dict[str, Optional[str]], key: str
) -> List[Any]:
    merchant_id = refs.get("merchant_id")
    conditions = [
        commerce_interactions.c.merchant_id == merchant_id,
        getattr(commerce_interactions.c, key) == refs.get(key),
    ]
    if key in _STORE_SCOPED_LOOKUP_KEYS:
        store_id = refs.get("store_id")
        conditions.append(
            commerce_interactions.c.store_id == store_id
            if store_id
            else commerce_interactions.c.store_id.is_(None)
        )
    return conditions


async def _lookup_matching_interactions(
    refs: Dict[str, Optional[str]],
) -> List[Dict[str, Any]]:
    """Return every interaction reached by the incoming stitch keys.

    Strong-key priority is deterministic. Weak keys are fallback-only and may
    confirm a strong match, but never nominate a destructive merge loser.
    """
    match_conditions = []
    # Strong keys are resolved first regardless of their legacy lookup order.
    # Weak keys may select one row only when no strong key matched at all.
    for key in _INTERACTION_SELECTION_KEYS:
        if not refs.get(key):
            continue
        # Merchant scope is shared outside the OR; the remaining predicates
        # retain per-key store scoping.
        conditions = _interaction_lookup_conditions(refs, key)[1:]
        match_conditions.append(and_(*conditions))
    if not match_conditions:
        return []

    rows = await database.fetch_all(
        select(commerce_interactions).where(
            commerce_interactions.c.merchant_id == refs.get("merchant_id"),
            or_(*match_conditions),
        )
    )

    candidates = sorted(
        (dict(row) for row in rows),
        key=lambda row: str(row.get("interaction_id") or ""),
    )
    matches: List[Dict[str, Any]] = []
    matched_ids: set[str] = set()
    for key in _INTERACTION_SELECTION_KEYS:
        value = refs.get(key)
        if not value:
            continue
        key_candidates = [row for row in candidates if row.get(key) == value]
        if not key_candidates:
            continue
        already_selected = next(
            (
                row
                for row in key_candidates
                if str(row.get("interaction_id") or "") in matched_ids
            ),
            None,
        )
        # A non-unique weak key (session/cart/payment) must not fan out and
        # collapse every interaction that shares it. Prefer an interaction
        # already selected by a stronger key. Once a strong match exists, a
        # weak key can confirm it but can never nominate a loser for deletion.
        if key in _WEAK_INTERACTION_LOOKUP_KEYS and matches and not already_selected:
            continue
        selected = already_selected or key_candidates[0]
        interaction_id = str(selected.get("interaction_id") or "")
        if interaction_id and interaction_id not in matched_ids:
            matched_ids.add(interaction_id)
            matches.append(selected)
    return matches


async def _lookup_existing_interaction(refs: Dict[str, Optional[str]]) -> Optional[Dict[str, Any]]:
    matches = await _lookup_matching_interactions(refs)
    return matches[0] if matches else None


_AGENT_IDENTITY_CONFIDENCE_RANK = {
    "unknown": 0,
    "browser_observed": 10,
    "merchant_asserted": 20,
    "platform_asserted": 30,
    "verified": 40,
}


def _known_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    return None if not text or text.lower() == "unknown" else text


def _identity_claim(metadata: Optional[Any]) -> tuple[Optional[str], str]:
    value = metadata if isinstance(metadata, dict) else {}
    agent_id = _known_text(value.get("agent_id"))
    confidence = str(value.get("agent_identity_confidence") or "unknown").strip().lower()
    if confidence not in _AGENT_IDENTITY_CONFIDENCE_RANK:
        confidence = "unknown"
    return agent_id, confidence


def _known_taxonomy(metadata: Optional[Any]) -> Dict[str, str]:
    value = metadata if isinstance(metadata, dict) else {}
    traffic = value.get("traffic") if isinstance(value.get("traffic"), dict) else {}
    return {
        field: known
        for field in TRAFFIC_TAXONOMY_FIELDS
        if field != "agent_id"
        and (known := _known_text(value.get(field)) or _known_text(traffic.get(field)))
    }


def _merge_metadata(existing: Optional[Any], patch: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    base = dict(existing or {}) if isinstance(existing, dict) else {}
    extra = _make_json_safe(patch or {}) if patch else {}
    existing_taxonomy = _known_taxonomy(base)
    incoming_taxonomy = _known_taxonomy(extra)
    existing_agent_id, existing_confidence = _identity_claim(base)
    incoming_agent_id, incoming_confidence = _identity_claim(extra)
    selected_agent_id = existing_agent_id
    selected_confidence = existing_confidence
    if incoming_agent_id and (
        not existing_agent_id
        or incoming_agent_id == existing_agent_id
        or _AGENT_IDENTITY_CONFIDENCE_RANK[incoming_confidence]
        > _AGENT_IDENTITY_CONFIDENCE_RANK[existing_confidence]
    ):
        selected_agent_id = incoming_agent_id
        if (
            incoming_agent_id != existing_agent_id
            or _AGENT_IDENTITY_CONFIDENCE_RANK[incoming_confidence]
            >= _AGENT_IDENTITY_CONFIDENCE_RANK[existing_confidence]
        ):
            selected_confidence = incoming_confidence
    if extra:
        base.update(extra)
    traffic = dict(base.get("traffic") or {}) if isinstance(base.get("traffic"), dict) else {}
    for field, existing_value in existing_taxonomy.items():
        if field not in incoming_taxonomy:
            base[field] = existing_value
            traffic[field] = existing_value
    if selected_agent_id:
        base["agent_id"] = selected_agent_id
        base["agent_identity_confidence"] = selected_confidence
        traffic["agent_id"] = selected_agent_id
    if traffic:
        base["traffic"] = traffic
    return base or None


def _authenticated_agent_id(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return an agent id only when the ingress authenticated that agent itself."""
    if not isinstance(metadata, dict):
        return None
    confidence = _normalize_text(metadata.get("agent_identity_confidence"))
    if confidence != "verified":
        return None
    return _normalize_text(metadata.get("agent_id"))


_STATUS_RANK = {
    "observed": 10,
    "discovery": 20,
    "pdp_viewed": 30,
    "indexed": 30,
    "blocked": 30,
    "surfaced": 35,
    "clicked": 40,
    "cart_active": 50,
    "checkout_started": 60,
    "checkout_created": 65,
    "checkout_submitted": 70,
    "awaiting_payment": 75,
    "payment_pending": 75,
    "payment_failed": 75,
    "payment_authorized": 80,
    "ordered": 85,
    "paid": 90,
    "cancelled": 100,
    "refund_pending": 110,
    "refunded": 120,
    "return_pending": 130,
    "return_synced": 140,
}


def _status_from_event(event_type: str, current: Optional[str]) -> Optional[str]:
    mapping = {
        "agent.requested": "observed",
        "search.performed": "discovery",
        "product.viewed": "pdp_viewed",
        "cart.created": "cart_active",
        "cart.item_added": "cart_active",
        "cart.item_removed": "cart_active",
        "cart.updated": "cart_active",
        "checkout.started": "checkout_started",
        "checkout.submitted": "checkout_submitted",
        "payment.attempted": "payment_pending",
        "payment.authorized": "payment_authorized",
        "payment.declined": "payment_failed",
        "payment.succeeded": "paid",
        "payment.failed": "payment_failed",
        "listing.exported": "indexed",
        "listing.blocked": "blocked",
        "surface.impression": "surfaced",
        "surface.click": "clicked",
        "checkout.created": "checkout_created",
        "payment.intent.created": "awaiting_payment",
        "payment.intent.viewed": "awaiting_payment",
        "order.created": "ordered",
        "order.paid": "paid",
        "order.cancelled": "cancelled",
        "refund.created": "refund_pending",
        "refund.requested": "refund_pending",
        "refund.succeeded": "refunded",
        "return.created": "return_pending",
        "return.completed": "return_synced",
        "return.sync.completed": "return_synced",
    }
    candidate = mapping.get(event_type)
    if candidate is None:
        return current
    if current is None:
        return candidate
    if _STATUS_RANK.get(candidate, 0) < _STATUS_RANK.get(current, 0):
        return current
    return candidate


def _is_unique_violation(exc: BaseException) -> bool:
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == "23505" or getattr(current, "pgcode", None) == "23505":
            return True
        if current.__class__.__name__ == "UniqueViolationError":
            return True
        if current.__class__.__name__ == "IntegrityError":
            message = str(current).lower()
            if "unique" in message or "duplicate" in message:
                return True
        current = getattr(current, "__cause__", None) or getattr(current, "orig", None)
    return False


async def _execute_insert(query: Any) -> Any:
    """Keep a PostgreSQL constraint failure inside a recoverable savepoint."""
    if IS_POSTGRES:
        async with database.transaction():
            return await database.execute(query)
    return await database.execute(query)


async def _execute_update(query: Any) -> Any:
    """Keep a PostgreSQL constraint failure inside a recoverable savepoint."""
    if IS_POSTGRES:
        async with database.transaction():
            return await database.execute(query)
    return await database.execute(query)


_MERGEABLE_INTERACTION_FIELDS = (
    "platform",
    "store_id",
    "surface",
    "commerce_surface",
    "prompt_id",
    "result_id",
    "click_id",
    "cart_id",
    "quote_id",
    "checkout_id",
    "payment_id",
    "order_id",
    "refund_id",
    "return_id",
    "canonical_product_id",
    "canonical_variant_id",
    "trace_id",
    "brief_id",
    "session_id",
    "visitor_id",
    "buyer_id",
    "source_channel",
    "source_family",
    "query_source",
    "agent_id",
    "protocol_name",
    "llm_provider",
    "llm_model",
    "caller_id",
)


def _merged_interaction_values(
    matches: List[Dict[str, Any]], values: Dict[str, Any]
) -> Dict[str, Any]:
    """Combine loser state into winner values without overriding fresh refs."""
    merged = dict(values)
    incoming_latest = (values.get("last_occurred_at"), values.get("latest_event_type"))
    for field in _MERGEABLE_INTERACTION_FIELDS:
        if merged.get(field) is None:
            merged[field] = next(
                (row.get(field) for row in matches if row.get(field) is not None),
                None,
            )

    metadata: Optional[Dict[str, Any]] = None
    # Lower-priority rows are folded first; winner and the incoming patch retain
    # the same precedence as the legacy update path.
    for row in reversed(matches):
        if isinstance(row.get("metadata"), dict):
            metadata = _merge_metadata(metadata, row["metadata"])
    if isinstance(values.get("metadata"), dict):
        metadata = _merge_metadata(metadata, values["metadata"])
    merged["metadata"] = metadata
    merged_agent_id, _ = _identity_claim(metadata)
    if merged_agent_id:
        merged["agent_id"] = merged_agent_id

    first_candidates = [
        value
        for value in [merged.get("first_occurred_at"), *(row.get("first_occurred_at") for row in matches)]
        if value is not None
    ]
    last_candidates = [
        value
        for value in [merged.get("last_occurred_at"), *(row.get("last_occurred_at") for row in matches)]
        if value is not None
    ]
    if first_candidates:
        merged["first_occurred_at"] = min(first_candidates)
    if last_candidates:
        merged["last_occurred_at"] = max(last_candidates)

    statuses = [merged.get("status"), *(row.get("status") for row in matches)]
    merged["status"] = max(
        (status for status in statuses if status is not None),
        key=lambda status: _STATUS_RANK.get(status, 0),
        default=None,
    )

    latest_candidates = [
        incoming_latest,
        *((row.get("last_occurred_at"), row.get("latest_event_type")) for row in matches),
    ]
    dated_latest = [candidate for candidate in latest_candidates if candidate[0] is not None and candidate[1]]
    if dated_latest:
        merged["latest_event_type"] = max(dated_latest, key=lambda candidate: candidate[0])[1]
    return merged


async def _merge_interactions(
    matches: List[Dict[str, Any]], values: Dict[str, Any]
) -> Dict[str, Any]:
    if len(matches) < 2:
        raise ValueError("interaction merge requires at least two matches")
    winner = matches[0]
    winner_id = str(winner["interaction_id"])
    loser_ids = [str(row["interaction_id"]) for row in matches[1:]]
    target_store_id = _normalize_text(values.get("store_id"))
    if any(_normalize_text(row.get("store_id")) != target_store_id for row in matches):
        raise ValueError("cannot merge commerce interactions across stores")
    merged_values = _merged_interaction_values(matches, values)
    merged_values["interaction_id"] = winner_id

    async with database.transaction():
        await database.execute(
            commerce_interaction_events.update()
            .where(commerce_interaction_events.c.interaction_id.in_(loser_ids))
            .values(interaction_id=winner_id)
        )
        await database.execute(
            commerce_interactions.delete().where(
                commerce_interactions.c.merchant_id == winner["merchant_id"],
                commerce_interactions.c.interaction_id.in_(loser_ids),
            )
        )
        await database.execute(
            commerce_interactions.update()
            .where(
                commerce_interactions.c.merchant_id == winner["merchant_id"],
                commerce_interactions.c.interaction_id == winner_id,
            )
            .values(**merged_values)
        )
    logger.info(
        "Merged commerce interactions merchant_id=%s winner_id=%s loser_ids=%s",
        winner["merchant_id"],
        winner_id,
        loser_ids,
    )
    row = await database.fetch_one(
        select(commerce_interactions).where(commerce_interactions.c.interaction_id == winner_id)
    )
    return dict(row) if row else {"interaction_id": winner_id, **merged_values}


def _stitch_advisory_lock_keys(
    merchant_id: str, refs: Dict[str, Optional[str]]
) -> List[str]:
    store_scope = refs.get("store_id") or ""
    return sorted(
        f"stitch|{merchant_id}|{store_scope}|{ref_name}|{refs[ref_name]}"
        for ref_name in _INTERACTION_LOOKUP_KEYS
        if refs.get(ref_name)
    )


@asynccontextmanager
async def _event_write_lock(
    merchant_id: str,
    event_type: str,
    key: Optional[str],
    refs: Dict[str, Optional[str]],
):
    if not IS_POSTGRES:
        yield
        return
    stitch_lock_keys = _stitch_advisory_lock_keys(merchant_id, refs)
    for _attempt in range(3):
        retry = False
        async with database.transaction():
            if key:
                await database.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(:lock_key))",
                    {"lock_key": f"event|{merchant_id}|{event_type}|{key}"},
                )
            # These locks are independent of mutable database state, so a
            # waiter cannot resolve a loser before a concurrent merge commits.
            for stitch_lock_key in stitch_lock_keys:
                await database.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(:lock_key))",
                    {"lock_key": stitch_lock_key},
                )

            matches = await _lookup_matching_interactions(refs)
            interaction_ids = {
                str(match["interaction_id"])
                for match in matches
                if match.get("interaction_id")
            }
            if not interaction_ids:
                interaction_ids.add(refs.get("interaction_id") or _fallback_interaction_id(refs))
            # Globally stable lock order prevents bridge events from taking the
            # same pair of interactions in opposite order.
            for interaction_id in sorted(interaction_ids):
                await database.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(:lock_key))",
                    {"lock_key": f"interaction|{merchant_id}|{interaction_id}"},
                )

            refreshed_matches = await _lookup_matching_interactions(refs)
            refreshed_ids = {
                str(match["interaction_id"])
                for match in refreshed_matches
                if match.get("interaction_id")
            }
            if refreshed_ids.issubset(interaction_ids):
                yield
                return
            retry = True
        if retry:
            continue
    raise RuntimeError("commerce interaction locks did not stabilize")


async def ensure_interaction(
    *,
    metadata: Optional[Dict[str, Any]] = None,
    first_occurred_at: Optional[datetime] = None,
    last_occurred_at: Optional[datetime] = None,
    latest_event_type: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    taxonomy = build_traffic_taxonomy(
        metadata,
        metadata=kwargs,
        authenticated_agent_id=_authenticated_agent_id(metadata),
        caller_id=_normalize_text(kwargs.get("caller_id")),
        default_source_channel=_normalize_text(kwargs.get("source_channel")),
        default_query_source=_normalize_text(kwargs.get("query_source")),
        default_protocol_name=_normalize_text(kwargs.get("protocol_name")),
        default_commerce_surface=(
            _normalize_text((metadata or {}).get("surface")) if isinstance(metadata, dict) else None
        ) or _normalize_text(kwargs.get("surface")),
    )
    metadata_with_taxonomy = attach_traffic_taxonomy(metadata, taxonomy)
    refs = _coerce_refs(metadata_with_taxonomy, **kwargs)
    merchant_id = refs.get("merchant_id")
    if not merchant_id:
        raise ValueError("merchant_id is required for commerce interaction tracking")

    matches = await _lookup_matching_interactions(refs)
    existing = matches[0] if matches else None
    interaction_id = (
        (existing or {}).get("interaction_id")
        or refs.get("interaction_id")
        or _fallback_interaction_id(refs)
    )
    now = _now()
    first_seen = first_occurred_at or now
    last_seen = last_occurred_at or first_seen
    existing_last = (existing or {}).get("last_occurred_at")
    incoming_is_latest = existing_last is None or last_seen >= existing_last
    status = _status_from_event(latest_event_type or "", (existing or {}).get("status"))
    merged_metadata = _merge_metadata(
        (existing or {}).get("metadata"), metadata_with_taxonomy
    )
    merged_agent_id, _ = _identity_claim(merged_metadata)
    values: Dict[str, Any] = {
        "interaction_id": interaction_id,
        "merchant_id": merchant_id,
        "platform": refs.get("platform") or (existing or {}).get("platform"),
        "store_id": refs.get("store_id") or (existing or {}).get("store_id"),
        "surface": refs.get("surface") or (existing or {}).get("surface"),
        "commerce_surface": _known_text(taxonomy.get("commerce_surface")) or (existing or {}).get("commerce_surface"),
        "prompt_id": refs.get("prompt_id") or (existing or {}).get("prompt_id"),
        "result_id": refs.get("result_id") or (existing or {}).get("result_id"),
        "click_id": refs.get("click_id") or (existing or {}).get("click_id"),
        "cart_id": refs.get("cart_id") or (existing or {}).get("cart_id"),
        "quote_id": refs.get("quote_id") or (existing or {}).get("quote_id"),
        "checkout_id": refs.get("checkout_id") or (existing or {}).get("checkout_id"),
        "payment_id": refs.get("payment_id") or (existing or {}).get("payment_id"),
        "order_id": refs.get("order_id") or (existing or {}).get("order_id"),
        "refund_id": refs.get("refund_id") or (existing or {}).get("refund_id"),
        "return_id": refs.get("return_id") or (existing or {}).get("return_id"),
        "canonical_product_id": refs.get("canonical_product_id") or (existing or {}).get("canonical_product_id"),
        "canonical_variant_id": refs.get("canonical_variant_id") or (existing or {}).get("canonical_variant_id"),
        "trace_id": refs.get("trace_id") or (existing or {}).get("trace_id"),
        "brief_id": refs.get("brief_id") or (existing or {}).get("brief_id"),
        "session_id": refs.get("session_id") or (existing or {}).get("session_id"),
        "visitor_id": refs.get("visitor_id") or (existing or {}).get("visitor_id"),
        "buyer_id": refs.get("buyer_id") or (existing or {}).get("buyer_id"),
        "source_channel": _known_text(taxonomy.get("source_channel")) or (existing or {}).get("source_channel"),
        "source_family": _known_text(taxonomy.get("source_family")) or (existing or {}).get("source_family"),
        "query_source": _known_text(taxonomy.get("query_source")) or (existing or {}).get("query_source"),
        "agent_id": merged_agent_id or _known_text((existing or {}).get("agent_id")),
        "protocol_name": _known_text(taxonomy.get("protocol_name")) or (existing or {}).get("protocol_name"),
        "llm_provider": _known_text(taxonomy.get("llm_provider")) or (existing or {}).get("llm_provider"),
        "llm_model": _known_text(taxonomy.get("llm_model")) or (existing or {}).get("llm_model"),
        "caller_id": _known_text(taxonomy.get("caller_id")) or (existing or {}).get("caller_id"),
        "latest_event_type": (
            latest_event_type if incoming_is_latest and latest_event_type else (existing or {}).get("latest_event_type")
        ),
        "status": status,
        "metadata": merged_metadata,
        "first_occurred_at": min(
            [dt for dt in [first_seen, (existing or {}).get("first_occurred_at")] if dt is not None]
        ),
        "last_occurred_at": max(
            [dt for dt in [last_seen, (existing or {}).get("last_occurred_at")] if dt is not None]
        ),
        "updated_at": now,
    }

    if len(matches) > 1:
        return await _merge_interactions(matches, values)
    if existing:
        try:
            await _execute_update(
                commerce_interactions.update()
                .where(commerce_interactions.c.interaction_id == interaction_id)
                .values(**values)
            )
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            raced_matches = await _lookup_matching_interactions(refs)
            if len(raced_matches) < 2:
                raise
            return await _merge_interactions(raced_matches, values)
    else:
        try:
            await _execute_insert(
                commerce_interactions.insert().values(
                    created_at=now,
                    **values,
                )
            )
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            raced = await _lookup_existing_interaction(refs)
            if not raced:
                raise
            return await ensure_interaction(
                metadata=metadata,
                first_occurred_at=first_occurred_at,
                last_occurred_at=last_occurred_at,
                latest_event_type=latest_event_type,
                **kwargs,
            )

    row = await database.fetch_one(
        select(commerce_interactions).where(commerce_interactions.c.interaction_id == interaction_id)
    )
    return dict(row) if row else {"interaction_id": interaction_id, **values}


async def record_commerce_event(
    *,
    event_type: str,
    metadata: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
    source: Optional[str] = None,
    upstream_idempotency_key: Optional[str] = None,
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    write_path: Optional[str] = None,
    authority: Optional[str] = None,
    agent_identity_confidence: Optional[str] = None,
    synthetic: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Append one event to the ledger.

    ``write_path`` / ``authority`` / ``agent_identity_confidence`` / ``synthetic``
    are trust provenance stamped by the authenticated ingress. They are stored
    as first-class columns and are deliberately NOT read from ``metadata``, so
    a caller-supplied payload cannot claim a standing its route did not grant.
    When ``write_path`` is given the authority is derived from it here; a
    caller may name the same authority but never a different one.
    """
    if write_path is not None:
        derived_authority = resolve_ledger_authority(
            write_path, str(agent_identity_confidence or "")
        )
        if authority is not None and authority != derived_authority:
            raise ValueError(
                f"write_path {write_path} carries authority {derived_authority}, not {authority}"
            )
        authority = derived_authority
    taxonomy = build_traffic_taxonomy(
        metadata,
        metadata=kwargs,
        authenticated_agent_id=_authenticated_agent_id(metadata),
        caller_id=_normalize_text(kwargs.get("caller_id")),
        default_source_channel=_normalize_text(kwargs.get("source_channel")),
        default_query_source=_normalize_text(kwargs.get("query_source")),
        default_protocol_name=_normalize_text(kwargs.get("protocol_name")),
        default_commerce_surface=(
            _normalize_text((metadata or {}).get("surface")) if isinstance(metadata, dict) else None
        ) or _normalize_text(kwargs.get("surface")),
    )
    metadata_with_taxonomy = attach_traffic_taxonomy(metadata, taxonomy)
    refs = _coerce_refs(metadata_with_taxonomy, **kwargs)
    merchant_id = refs.get("merchant_id")
    if not merchant_id:
        raise ValueError("merchant_id is required for commerce interaction tracking")

    occurred = occurred_at or _now()
    async with _event_write_lock(merchant_id, event_type, upstream_idempotency_key, refs):
        return await _record_commerce_event_unlocked(
            event_type=event_type,
            metadata_with_taxonomy=metadata_with_taxonomy,
            occurred=occurred,
            source=source,
            upstream_idempotency_key=upstream_idempotency_key,
            actor_type=actor_type,
            actor_id=actor_id,
            refs=refs,
            write_path=write_path,
            authority=authority,
            agent_identity_confidence=agent_identity_confidence,
            synthetic=synthetic,
        )


async def _record_commerce_event_unlocked(
    *,
    event_type: str,
    metadata_with_taxonomy: Optional[Dict[str, Any]],
    occurred: datetime,
    source: Optional[str],
    upstream_idempotency_key: Optional[str],
    actor_type: Optional[str],
    actor_id: Optional[str],
    refs: Dict[str, Optional[str]],
    write_path: Optional[str] = None,
    authority: Optional[str] = None,
    agent_identity_confidence: Optional[str] = None,
    synthetic: bool = False,
) -> Dict[str, Any]:
    merchant_id = refs.get("merchant_id")
    if not merchant_id:
        raise ValueError("merchant_id is required for commerce interaction tracking")

    duplicate_event = None
    if upstream_idempotency_key:
        duplicate_event = await database.fetch_one(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.merchant_id == merchant_id,
                commerce_interaction_events.c.event_type == event_type,
                commerce_interaction_events.c.upstream_idempotency_key == upstream_idempotency_key,
            )
        )
    if duplicate_event:
        duplicate = dict(duplicate_event)
        return {
            "interaction_id": duplicate.get("interaction_id"),
            "event_id": duplicate.get("event_id"),
            "duplicate": True,
        }

    interaction = await ensure_interaction(
        metadata=metadata_with_taxonomy,
        first_occurred_at=occurred,
        last_occurred_at=occurred,
        latest_event_type=event_type,
        **refs,
    )

    event_id = (
        _stable_id("evt", merchant_id, event_type, upstream_idempotency_key)
        if upstream_idempotency_key
        else f"evt_{uuid.uuid4().hex[:24]}"
    )
    payload = _make_json_safe(
        {
            **(metadata_with_taxonomy or {}),
            **{key: value for key, value in refs.items() if value},
        }
    )
    try:
        await _execute_insert(
            commerce_interaction_events.insert().values(
                event_id=event_id,
                interaction_id=interaction["interaction_id"],
                merchant_id=merchant_id,
                platform=refs.get("platform") or interaction.get("platform"),
                store_id=refs.get("store_id") or interaction.get("store_id"),
                surface=refs.get("surface") or interaction.get("surface"),
                event_type=event_type,
                occurred_at=occurred,
                canonical_product_id=refs.get("canonical_product_id") or interaction.get("canonical_product_id"),
                canonical_variant_id=refs.get("canonical_variant_id") or interaction.get("canonical_variant_id"),
                trace_id=refs.get("trace_id") or interaction.get("trace_id"),
                brief_id=refs.get("brief_id") or interaction.get("brief_id"),
                session_id=refs.get("session_id") or interaction.get("session_id"),
                visitor_id=refs.get("visitor_id") or interaction.get("visitor_id"),
                cart_id=refs.get("cart_id") or interaction.get("cart_id"),
                payment_id=refs.get("payment_id") or interaction.get("payment_id"),
                source=source,
                upstream_idempotency_key=upstream_idempotency_key,
                actor_type=actor_type,
                actor_id=actor_id,
                write_path=_normalize_text(write_path),
                authority=_normalize_text(authority),
                agent_identity_confidence=_normalize_text(agent_identity_confidence),
                synthetic=bool(synthetic),
                payload=payload,
            )
        )
    except Exception as exc:
        if not upstream_idempotency_key or not _is_unique_violation(exc):
            raise
        duplicate_event = await database.fetch_one(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.merchant_id == merchant_id,
                commerce_interaction_events.c.event_type == event_type,
                commerce_interaction_events.c.upstream_idempotency_key == upstream_idempotency_key,
            )
        )
        if not duplicate_event:
            raise
        duplicate = dict(duplicate_event)
        return {
            "interaction_id": duplicate.get("interaction_id"),
            "event_id": duplicate.get("event_id"),
            "duplicate": True,
        }
    return {
        "interaction_id": interaction["interaction_id"],
        "event_id": event_id,
        "duplicate": False,
    }


async def record_commerce_event_best_effort(
    *,
    event_type: str,
    metadata: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
    source: Optional[str] = None,
    upstream_idempotency_key: Optional[str] = None,
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    write_path: Optional[str] = None,
    authority: Optional[str] = None,
    agent_identity_confidence: Optional[str] = None,
    synthetic: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    refs = _coerce_refs(metadata, **kwargs)
    interaction_id = refs.get("interaction_id") or _fallback_interaction_id(refs)
    fallback_event_id = (
        _stable_id("evt", refs.get("merchant_id"), event_type, upstream_idempotency_key)
        if upstream_idempotency_key
        else f"evt_{uuid.uuid4().hex[:24]}"
    )
    try:
        return await record_commerce_event(
            event_type=event_type,
            metadata=metadata,
            occurred_at=occurred_at,
            source=source,
            upstream_idempotency_key=upstream_idempotency_key,
            actor_type=actor_type,
            actor_id=actor_id,
            write_path=write_path,
            authority=authority,
            agent_identity_confidence=agent_identity_confidence,
            synthetic=synthetic,
            **kwargs,
        )
    except Exception as exc:
        logger.warning(
            "Best-effort commerce ledger write skipped for event_type=%s merchant_id=%s: %s",
            event_type,
            refs.get("merchant_id"),
            exc,
        )
        return {
            "interaction_id": interaction_id,
            "event_id": fallback_event_id,
            "duplicate": False,
            "degraded": True,
        }


async def find_interaction_by_checkout_id(
    checkout_id: str,
    *,
    merchant_id: str,
    store_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    conditions = [
        commerce_interactions.c.merchant_id == str(merchant_id).strip(),
        commerce_interactions.c.checkout_id == str(checkout_id).strip(),
    ]
    if store_id:
        conditions.append(commerce_interactions.c.store_id == str(store_id).strip())
    row = await database.fetch_one(
        select(commerce_interactions).where(*conditions)
    )
    return dict(row) if row else None


async def find_interaction_by_order_id(
    order_id: str,
    *,
    merchant_id: str,
    store_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    conditions = [
        commerce_interactions.c.merchant_id == str(merchant_id).strip(),
        commerce_interactions.c.order_id == str(order_id).strip(),
    ]
    if store_id:
        conditions.append(commerce_interactions.c.store_id == str(store_id).strip())
    row = await database.fetch_one(
        select(commerce_interactions).where(*conditions)
    )
    return dict(row) if row else None


async def trace_interaction(interaction_id: str) -> Dict[str, Any]:
    interaction_row = await database.fetch_one(
        select(commerce_interactions).where(commerce_interactions.c.interaction_id == str(interaction_id).strip())
    )
    event_rows = await database.fetch_all(
        select(commerce_interaction_events)
        .where(commerce_interaction_events.c.interaction_id == str(interaction_id).strip())
        .order_by(commerce_interaction_events.c.occurred_at.asc(), commerce_interaction_events.c.created_at.asc())
    )
    return {
        "interaction": dict(interaction_row) if interaction_row else None,
        "events": [dict(row) for row in event_rows],
    }
