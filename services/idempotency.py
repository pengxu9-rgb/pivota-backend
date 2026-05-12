"""Audit idempotency key computation (P1.1 utility, used by Phase 2).

Computes a stable key for a given audit submission so duplicate
clicks within a short window don't re-execute the work.

Key formula:
    sha256(merchant_id + sorted(product_keys) + window_minute_floor)

Phase 2 uses this in `POST /api/audits` to dedupe submissions:
    1. Compute key from request
    2. Look up `audit_runs` WHERE idempotency_key = key AND
       stage NOT IN ('completed', 'failed', 'cancelled')
    3. If found → return existing run id with `dedup_hit=true`
    4. Otherwise → create new run with `idempotency_key = key`

The 5-minute window is the locked Phase-2 default (Phase plan
decision #3). Don't make this configurable until a real merchant
asks for it.

P1.1 ships the helper as a no-op-callable utility so Phase 2 can
land it later without wiring concerns. Tests can land in Phase 1
to lock the formula before consumers exist.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Iterable, Optional


DEFAULT_WINDOW_MINUTES = 5


def compute_audit_idempotency_key(
    *,
    merchant_id: str,
    product_keys: Optional[Iterable[str]] = None,
    subject_type: str = "merchant",
    submitted_at: Optional[datetime] = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> str:
    """Return a sha256 hex digest that uniquely identifies an audit
    submission within a time window.

    Args:
      merchant_id: stable id (real merchant_id or `prospect_<hash>`
        for cold-start). Required.
      product_keys: ordered or unordered list of product identifiers
        in the audit. Sorted internally so order doesn't matter.
        None → empty list (treated as same as empty).
      subject_type: `merchant` or `cold_start` — keeps merchant
        self-audits distinct from BD cold-start audits even when the
        merchant_id collides with a synthetic prospect id.
      submitted_at: defaults to now (UTC). Floored to the start of
        the current `window_minutes` window so two clicks 30s apart
        produce the same key, two clicks 6min apart produce
        different keys.
      window_minutes: dedup window. 5 by default; don't change
        without filing an ADR.

    Returns: 64-char lowercase hex string.

    Raises: ValueError when merchant_id is empty.
    """
    if not merchant_id or not str(merchant_id).strip():
        raise ValueError("merchant_id is required for idempotency key")
    if window_minutes < 1:
        raise ValueError("window_minutes must be >= 1")

    submitted_at = submitted_at or datetime.now(timezone.utc)
    if submitted_at.tzinfo is None:
        # Coerce naive timestamps to UTC — caller bug, but recovering
        # rather than crashing is the right behavior for a utility.
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)

    # Floor to the window boundary. Example: window=5min, now=12:07:23
    # → window_floor=12:05:00. Two submissions at 12:07 and 12:09
    # produce the same window; 12:07 + 12:11 produce different.
    floored_minute = (submitted_at.minute // window_minutes) * window_minutes
    window_floor = submitted_at.replace(
        minute=floored_minute, second=0, microsecond=0,
    )
    window_floor_iso = window_floor.isoformat()

    # Sort product_keys so ['a', 'b'] and ['b', 'a'] dedupe the same
    sorted_products = sorted(str(p) for p in (product_keys or []) if p)
    products_joined = ",".join(sorted_products)

    raw = (
        f"{subject_type}|{merchant_id}|{products_joined}|{window_floor_iso}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
